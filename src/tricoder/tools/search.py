"""工作区文本搜索与 glob 定位工具。"""

from __future__ import annotations

import os
import re
import json
import sys
import time
from pathlib import Path
from typing import Any

from tricoder.models import ToolResult, tool_failure
from tricoder.execution_state import ErrorCode, RecoveryAction
from tricoder.policy import PolicyError
from tricoder.core.cancellation import CancellationToken, check_current_cancellation, current_cancellation
from tricoder.process.control import run_bounded_process
from tricoder.process.env import filtered_subprocess_env
from tricoder.task_cleanup import run_in_cleanup_thread

from tricoder.tools.gitignore import _GitIgnoreMatcher
from tricoder.tools.handlers import ToolHandler


_MAX_GLOB_MATCHES = 500
_MAX_GLOB_SCAN = 2_000
_MAX_GLOB_PATTERN_CHARS = 200
_MAX_GLOB_STARS = 2
_MAX_REGEX_CHARS = 200
_MAX_SEARCH_LINE_CHARS = 4_096
_MAX_SEARCH_ENTRIES = 10_000
_MAX_SEARCH_BYTES = 16 * 1024 * 1024
_ABSOLUTE_PATH_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


class SearchTextTool(ToolHandler):
    name = "search_text"
    description = "在工作区目录内搜索文本。"
    parameters = ToolHandler._schema(
        {
            "path": {"type": "string"},
            "query": {"type": "string"},
            "use_regex": {"type": "boolean"},
        },
        ["query"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return self._run(arguments, current_cancellation())

    async def run_async(self, arguments, *, cancellation=None):
        return await run_in_cleanup_thread(self._run, arguments, cancellation)

    def _run(self, arguments, cancellation: CancellationToken | None) -> ToolResult:
        deadline = time.monotonic() + min(self.context.timeout, 10)

        def check_budget():
            check_current_cancellation()
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError

        try:
            return self._search(arguments, cancellation, deadline, check_budget)
        except TimeoutError:
            return tool_failure(ErrorCode.TIMEOUT, "搜索预算耗尽，未完成搜索", recovery=RecoveryAction.REPLAN)

    def _search(self, arguments, cancellation, deadline, check_budget):
        query = self._required_str(arguments, "query")
        use_regex = arguments.get("use_regex", False)
        if not isinstance(use_regex, bool):
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "use_regex 必须是布尔值")
        if use_regex:
            if len(query) > _MAX_REGEX_CHARS:
                return tool_failure(
                    ErrorCode.INVALID_ARGUMENT,
                    f"正则表达式过长（最多 {_MAX_REGEX_CHARS} 字符），"
                    "防止灾难性回溯拖慢 Agent",
                )
            try:
                re.compile(query)
            except re.error:
                return tool_failure(ErrorCode.INVALID_ARGUMENT, "正则表达式无效")
        root = self.context.workspace_policy.resolve_path(str(arguments.get("path", ".")))
        if not root.is_dir():
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "search_text 的 path 必须是目录")

        workspace = self.context.workspace_policy.workspace
        ignore_matcher = _GitIgnoreMatcher(workspace)
        matches: list[str] = []
        records = []
        stack = [root]
        entries = total_bytes = 0
        # scandir 逐项计数，避免 os.walk 先物化整个大型目录。
        while stack:
            check_budget()
            with os.scandir(stack.pop()) as iterator:
                for entry in iterator:
                    check_budget()
                    entries += 1
                    if entries > _MAX_SEARCH_ENTRIES:
                        return tool_failure(ErrorCode.EXECUTION_FAILED, "搜索条目预算耗尽，未完成搜索")
                    try:
                        if entry.is_symlink():
                            continue
                        path = self.context.workspace_policy.resolve_path(entry.path)
                    except PolicyError:
                        continue
                    relative = path.relative_to(workspace).as_posix()
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if ignore_matcher.is_ignored(relative + ("/" if is_directory else "")):
                        continue
                    if is_directory:
                        stack.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        if path.stat().st_size > self.context.max_search_file_bytes:
                            continue
                        limit = min(self.context.max_search_file_bytes, _MAX_SEARCH_BYTES - total_bytes)
                        with path.open("rb") as stream:
                            raw = stream.read(limit + 1)
                        total_bytes += len(raw)
                        if total_bytes > _MAX_SEARCH_BYTES:
                            return tool_failure(ErrorCode.EXECUTION_FAILED, "搜索字节预算耗尽，未完成搜索")
                        if len(raw) > self.context.max_search_file_bytes or b"\x00" in raw:
                            continue
                        text = raw.decode("utf-8")
                    except (OSError, UnicodeError):
                        continue
                    check_budget()
                    if use_regex:
                        records.append((relative, text))
                        continue
                    for number, line in enumerate(text.splitlines(), 1):
                        check_budget()
                        if len(line) <= _MAX_SEARCH_LINE_CHARS and query in line:
                            matches.append(f"{relative}:{number}: {line.strip()}")
                            if len(matches) == 200:
                                return ToolResult(True, self._bounded("\n".join(matches) + "\n（已截断：200 条匹配）"))
        if use_regex:
            payload = json.dumps({"query": query, "files": records}, ensure_ascii=False).encode("utf-8")
            if len(payload) > 32 * 1024 * 1024:
                return tool_failure(ErrorCode.EXECUTION_FAILED, "搜索输入预算耗尽，未完成搜索")
            check_budget()
            helper = Path(__file__).resolve().parents[1] / "process" / "regex_worker.py"
            result = run_bounded_process(
                [sys.executable, "-I", "-S", str(helper)], cwd=helper.parent,
                env=filtered_subprocess_env(excluded_paths=(workspace,)),
                timeout=deadline-time.monotonic(), max_output_bytes=8*1024*1024,
                cancellation=cancellation, stdin_bytes=payload,
                recoverable_timeout=True,
            )
            if result.cleanup_failed:
                return tool_failure(ErrorCode.CLEANUP_FAILED, "搜索进程清理未确认")
            if result.timed_out:
                return tool_failure(ErrorCode.TIMEOUT, "正则搜索超时，未完成搜索", recovery=RecoveryAction.REPLAN)
            if result.returncode or result.output_exceeded or result.input_failed:
                return tool_failure(ErrorCode.EXECUTION_FAILED, "搜索子进程未完整返回结果")
            try:
                data = json.loads(result.stdout)
                if not isinstance(data, dict) or set(data) != {"matches", "truncated"}:
                    raise ValueError
                matches = data["matches"]
                if not isinstance(matches, list) or len(matches) > 200 or not all(isinstance(item, str) for item in matches) or type(data["truncated"]) is not bool:
                    raise ValueError
                if data["truncated"]:
                    matches.append("（已截断：200 条匹配）")
            except (ValueError, TypeError):
                return tool_failure(ErrorCode.EXECUTION_FAILED, "搜索子进程结果格式无效")
        return ToolResult(True, self._bounded("\n".join(matches) or "(无匹配)"))


class GlobFilesTool(ToolHandler):
    name = "glob_files"
    description = "按相对 glob 模式列出工作区内匹配的文件与目录。"
    parameters = ToolHandler._schema(
        {
            "path": {"type": "string"},
            "pattern": {"type": "string"},
        },
        ["pattern"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        root = self.context.workspace_policy.resolve_path(str(arguments.get("path", ".")))
        if not root.is_dir():
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "glob_files 的 path 必须是目录")
        pattern = self._required_str(arguments, "pattern")
        try:
            self._validate_glob_pattern(pattern)
        except ValueError:
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "glob 模式必须是受限的工作区相对模式")

        workspace = self.context.workspace_policy.workspace
        matches: list[str] = []
        scan_count = 0
        for match in root.glob(pattern):
            scan_count += 1
            if scan_count > _MAX_GLOB_SCAN:
                matches.append(f"...（扫描超过 {_MAX_GLOB_SCAN} 个结果，已停止）")
                break
            try:
                resolved = self.context.workspace_policy.resolve_path(match)
            except PolicyError:
                continue
            if not resolved.is_relative_to(workspace):
                continue
            suffix = "/" if resolved.is_dir() else ""
            matches.append(f"{resolved.relative_to(workspace).as_posix()}{suffix}")
            if len(matches) >= _MAX_GLOB_MATCHES:
                matches.append(f"...（已截断，仅显示前 {_MAX_GLOB_MATCHES} 条）")
                break
        return ToolResult(True, self._bounded("\n".join(matches) or "(无匹配)"))

    @staticmethod
    def _validate_glob_pattern(pattern: str) -> None:
        """glob 模式必须指向工作区内的相对路径且规模受控。"""
        if not pattern or pattern.startswith(("/", "\\")):
            raise ValueError("glob 模式必须是工作区内的相对路径")
        if _ABSOLUTE_PATH_PREFIX.match(pattern):
            raise ValueError("glob 模式必须是工作区内的相对路径")
        if ".." in re.split(r"[\\/]+", pattern):
            raise ValueError("glob 模式不能包含上级目录")
        if len(pattern) > _MAX_GLOB_PATTERN_CHARS:
            raise ValueError(f"glob 模式过长（最多 {_MAX_GLOB_PATTERN_CHARS} 字符）")
        if pattern.count("**") > _MAX_GLOB_STARS:
            raise ValueError(f"glob 模式最多允许 {_MAX_GLOB_STARS} 个 **")
