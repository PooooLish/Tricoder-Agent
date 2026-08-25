"""受限命令执行与任务完成工具。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from tricoder.models import ToolResult
from tricoder.policy import PolicyError

from tricoder.tools.handlers import ToolHandler


# 子进程环境过滤：剔除名称可能承载凭据的变量（API Key/token/password/secret 等）。
# 这是“最小、可解释”的过滤：保留其余环境，只剔除可疑命名，避免把 Provider
# 密钥、token、密码或授权头泄漏给被执行的测试/脚本/子进程。
_SENSITIVE_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|"
    r"token|password|passwd|secret|credential|authorization)"
)
_PRESERVED_ENV = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP",
        "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "COMSPEC",
        "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "LC_ALL", "LANG",
        "PYTHONUTF8", "PYTHONIOENCODING", "TERM", "COLORTERM",
    }
)


def _filtered_env() -> dict[str, str]:
    """返回剔除敏感凭据变量后的子进程环境。

    策略：默认保留完整环境，但剔除名称匹配敏感模式的变量（大小写不敏感）；
    同时显式保留已知必要的系统/构建变量。这是最小黑名单过滤，不伪装成沙盒。
    """
    env = os.environ.copy()
    for name in list(env):
        if name in _PRESERVED_ENV:
            continue
        if _SENSITIVE_ENV_RE.search(name):
            del env[name]
    return env


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
        auto_approved = (
            self.context.auto_approve_git is not None
            and self.context.auto_approve_git(args)
        )
        approved = auto_approved or self.context.approver("run_command", detail)
        if not approved:
            return ToolResult(False, "用户拒绝了命令执行")
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                env=_filtered_env(),
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
                env=_filtered_env(),
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
