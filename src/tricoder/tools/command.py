"""受限命令执行与任务完成工具。"""

from __future__ import annotations

import subprocess
from typing import Any

from tricoder.models import ToolResult

from tricoder.tools.handlers import ToolHandler


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


class FinishTool(ToolHandler):
    name = "finish"
    description = "提交本轮任务的文字总结。"
    parameters = ToolHandler._schema({"summary": {"type": "string"}}, ["summary"])

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(True, self._required_str(arguments, "summary"))
