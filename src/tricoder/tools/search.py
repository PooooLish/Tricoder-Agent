"""工作区文本搜索与 glob 定位工具。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tricoder.models import ToolResult
from tricoder.policy import PolicyError

from tricoder.tools.gitignore import _GitIgnoreMatcher
from tricoder.tools.handlers import ToolHandler


_MAX_GLOB_MATCHES = 500
_MAX_GLOB_SCAN = 2_000
_MAX_GLOB_PATTERN_CHARS = 200
_MAX_GLOB_STARS = 2
_MAX_REGEX_CHARS = 200
_MAX_SEARCH_LINE_CHARS = 4_096
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
        query = self._required_str(arguments, "query")
        use_regex = arguments.get("use_regex", False)
        if not isinstance(use_regex, bool):
            return ToolResult(False, "use_regex 必须是布尔值")
        if use_regex:
            if len(query) > _MAX_REGEX_CHARS:
                return ToolResult(
                    False,
                    f"正则表达式过长（最多 {_MAX_REGEX_CHARS} 字符），"
                    "防止灾难性回溯拖慢 Agent",
                )
            try:
                compiled = re.compile(query)
            except re.error as exc:
                return ToolResult(False, f"正则表达式无效：{exc}")
        root = self.context.workspace_policy.resolve_path(str(arguments.get("path", ".")))
        if not root.is_dir():
            return ToolResult(False, "search_text 的 path 必须是目录")

        workspace = self.context.workspace_policy.workspace
        ignore_matcher = _GitIgnoreMatcher(workspace)
        matches: list[str] = []
        for current_root, directories, files in os.walk(root, followlinks=False):
            current = Path(current_root)
            # 在进入子目录前过滤敏感目录、可能逃逸的链接和 .gitignore 忽略项。
            allowed_directories: list[str] = []
            for name in directories:
                try:
                    candidate = self.context.workspace_policy.resolve_path(current / name)
                except PolicyError:
                    continue
                if candidate.is_symlink():
                    continue
                if ignore_matcher.is_ignored(
                    candidate.relative_to(workspace).as_posix() + "/"
                ):
                    continue
                allowed_directories.append(name)
            directories[:] = allowed_directories

            for name in files:
                try:
                    path = self.context.workspace_policy.resolve_path(current / name)
                except PolicyError:
                    continue
                relative = path.relative_to(workspace).as_posix()
                if ignore_matcher.is_ignored(relative):
                    continue
                try:
                    if path.stat().st_size > self.context.max_search_file_bytes:
                        continue
                    raw = path.read_bytes()
                    if b"\x00" in raw:
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeError, ValueError):
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if len(line) > _MAX_SEARCH_LINE_CHARS:
                        continue
                    if use_regex:
                        found = compiled.search(line) is not None
                    else:
                        found = query in line
                    if not found:
                        continue
                    matches.append(f"{relative}:{line_number}: {line.strip()}")
                    if len(matches) >= 200:
                        return ToolResult(True, self._bounded("\n".join(matches)))
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
            return ToolResult(False, "glob_files 的 path 必须是目录")
        pattern = self._required_str(arguments, "pattern")
        try:
            self._validate_glob_pattern(pattern)
        except ValueError as exc:
            return ToolResult(False, str(exc))

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
