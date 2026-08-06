"""受限命令执行与任务完成工具。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tricoder.models import ToolResult
from tricoder.policy import PolicyError

from tricoder.tools.handlers import ToolHandler


def _git_toplevel(workspace: Path) -> Path | None:
    """返回 workspace 所在 git 仓库根；非 git 仓库返回 None。

    git 会沿目录树上溯查找 .git，因此在仓库子目录工作区运行 git 会读取
    工作区外的仓库历史与源码，必须由调用方校验并拒绝。
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def _git_command_escapes_workspace(cwd: Path) -> bool:
    """git 命令在 cwd 执行是否会越过工作区边界读取仓库根内容。"""
    root = _git_toplevel(cwd)
    return root is not None and root != cwd.resolve()


class RunCommandTool(ToolHandler):
    name = "run_command"
    description = "经审批后在工作区内运行受策略允许的命令。"
    parameters = ToolHandler._schema(
        {"command": {"type": "string"}, "cwd": {"type": "string"}},
        ["command"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止执行命令")
        command = self._required_str(arguments, "command")
        args = self.context.command_policy.validate(command)
        cwd = self.context.workspace_policy.resolve_path(str(arguments.get("cwd", ".")))
        if not cwd.is_dir():
            return ToolResult(False, "命令工作目录必须是目录")
        executable = Path(args[0]).name.lower().removesuffix(".exe")
        if executable == "git" and _git_command_escapes_workspace(cwd):
            return ToolResult(
                False,
                "git 仓库根超出工作区，拒绝执行（防止读取工作区外仓库内容）",
            )
        detail = (
            f"目录：{cwd}\n"
            f"命令：{command}\n"
            f"执行：{args[0]}\n"
            f"超时：{self.context.timeout:g} 秒"
        )
        if not self.context.approver("run_command", detail):
            return ToolResult(False, "用户拒绝了命令执行")
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.context.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"命令执行超过 {self.context.timeout:g} 秒")
        output = (
            f"退出码：{completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        return ToolResult(completed.returncode == 0, self._bounded(output))


class GitDiffTool(ToolHandler):
    """只读展示工作区未提交变更的 diff 统计，无需审批（策略已限只读）。"""

    name = "git_diff"
    description = "显示工作区未提交变更的只读 diff 统计。"
    parameters = ToolHandler._schema({})

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = self.context.workspace_policy.workspace
        if _git_command_escapes_workspace(workspace):
            return ToolResult(
                False,
                "git 仓库根超出工作区，拒绝执行（防止读取工作区外仓库内容）",
            )
        try:
            args = self.context.command_policy.validate("git --no-pager diff --stat")
        except PolicyError as exc:
            return ToolResult(False, str(exc))
        try:
            completed = subprocess.run(
                args,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.context.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"git diff 超过 {self.context.timeout:g} 秒")
        output = (completed.stdout or completed.stderr).strip()
        if not output:
            output = "工作区没有未提交变更"
        return ToolResult(completed.returncode == 0, self._bounded(output))


class FinishTool(ToolHandler):
    name = "finish"
    description = "提交本轮任务的文字总结。"
    parameters = ToolHandler._schema({"summary": {"type": "string"}}, ["summary"])

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(True, self._required_str(arguments, "summary"))
