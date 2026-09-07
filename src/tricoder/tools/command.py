"""受限命令执行与任务完成工具。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.models import ToolResult
from tricoder.policy import CommandPolicy, PolicyError
from tricoder.subprocess_env import filtered_subprocess_env
from tricoder.subprocess_control import run_bounded_process

from tricoder.tools.handlers import ToolHandler


_filtered_env = filtered_subprocess_env


def _git_toplevel(workspace: Path, command_policy: CommandPolicy) -> Path | None:
    """返回 workspace 所在 git 仓库根；非 git 仓库返回 None。

    git 会沿目录树上溯查找 .git，因此在仓库子目录工作区运行 git 会读取
    工作区外的仓库历史与源码，必须由调用方校验并拒绝。
    """
    try:
        git_executable = command_policy.validate("git status")[0]
        completed = subprocess.run(
            [git_executable, "rev-parse", "--show-toplevel"],
            cwd=workspace,
            env=command_policy.subprocess_environment(),
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


def _git_command_escapes_workspace(
    cwd: Path,
    command_policy: CommandPolicy,
) -> bool:
    """git 命令在 cwd 执行是否会越过工作区边界读取仓库根内容。"""
    root = _git_toplevel(cwd, command_policy)
    return root is not None and root != cwd.resolve()


class RunCommandTool(ToolHandler):
    name = "run_command"
    description = "经审批后在工作区内运行受策略允许的命令。"
    parameters = ToolHandler._schema(
        {"command": {"type": "string"}, "cwd": {"type": "string"}},
        ["command"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return self.run_with_cancellation(arguments, None)

    async def run_async(
        self,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        """在线程中运行受控命令，并把取消令牌传入进程树控制。"""

        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return await asyncio.to_thread(
            self.run_with_cancellation,
            arguments,
            cancellation,
        )

    def run_with_cancellation(
        self,
        arguments: dict[str, Any],
        cancellation: CancellationToken | None,
    ) -> ToolResult:
        """执行受控命令，并允许运行时取消信号终止整个进程树。"""

        if self.context.read_only:
            return ToolResult(False, "只读模式禁止执行命令")
        command = self._required_str(arguments, "command")
        args = self.context.command_policy.validate(command)
        subprocess_env = self.context.command_policy.subprocess_environment()
        cwd = self.context.workspace_policy.resolve_path(str(arguments.get("cwd", ".")))
        if not cwd.is_dir():
            return ToolResult(False, "命令工作目录必须是目录")
        executable = Path(args[0]).name.lower().removesuffix(".exe")
        if executable == "git" and _git_command_escapes_workspace(
            cwd,
            self.context.command_policy,
        ):
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
        auto_approved = (
            self.context.auto_approve_git is not None
            and self.context.auto_approve_git(args)
        )
        approved = auto_approved or self.context.approver("run_command", detail)
        if not approved:
            return ToolResult(False, "用户拒绝了命令执行")
        try:
            completed = run_bounded_process(
                args,
                cwd=cwd,
                env=subprocess_env,
                timeout=self.context.timeout,
                max_output_bytes=self.context.max_output_chars,
                cancellation=cancellation,
            )
        except CancellationError:
            raise
        except OSError:
            return ToolResult(False, "命令进程无法安全启动")
        if completed.cleanup_failed:
            return ToolResult(False, "命令进程树清理失败，结果不可信")
        if completed.timed_out:
            return ToolResult(False, f"命令执行超过 {self.context.timeout:g} 秒")
        if completed.output_exceeded:
            captured = self._bounded(
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
            return ToolResult(
                False,
                f"命令输出超过 {self.context.max_output_chars} 字符，已终止进程树\n"
                f"{captured}",
            )
        output = (
            f"退出码：{completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        succeeded = completed.returncode == 0
        return ToolResult(
            succeeded,
            self._bounded(output),
            verification_passed=(
                succeeded if _is_verification_command(args) else None
            ),
        )


_VERIFICATION_MODULES = {"unittest", "pytest", "compileall", "ruff", "mypy"}


def _is_verification_command(args: list[str]) -> bool:
    """只有认可的测试/编译/静态检查命令才产生验证结论。

    git 只读命令与普通脚本执行不改变验证状态。
    """
    executable = Path(args[0]).name.lower().removesuffix(".exe")
    return (
        executable in {"python", "py"}
        and len(args) >= 3
        and args[1] == "-m"
        and args[2].lower() in _VERIFICATION_MODULES
    )


class GitDiffTool(ToolHandler):
    """只读展示工作区未提交变更的 diff 统计，无需审批（策略已限只读）。"""

    name = "git_diff"
    description = "显示工作区未提交变更的只读 diff 统计。"
    parameters = ToolHandler._schema({})

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = self.context.workspace_policy.workspace
        if _git_command_escapes_workspace(workspace, self.context.command_policy):
            return ToolResult(
                False,
                "git 仓库根超出工作区，拒绝执行（防止读取工作区外仓库内容）",
            )
        try:
            args = self.context.command_policy.validate("git --no-pager diff --stat")
        except PolicyError as exc:
            return ToolResult(False, str(exc))
        try:
            completed = run_bounded_process(
                args,
                cwd=workspace,
                env=self.context.command_policy.subprocess_environment(),
                timeout=self.context.timeout,
                max_output_bytes=self.context.max_output_chars,
            )
        except OSError:
            return ToolResult(False, "git diff 进程无法安全启动")
        if completed.cleanup_failed:
            return ToolResult(False, "git diff 进程树清理失败，结果不可信")
        if completed.timed_out:
            return ToolResult(False, f"git diff 超过 {self.context.timeout:g} 秒")
        if completed.output_exceeded:
            return ToolResult(False, "git diff 输出超过限制，已终止进程树")
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
