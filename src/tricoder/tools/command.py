"""受限命令执行与任务完成工具。"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.task_cleanup import run_in_cleanup_thread
from tricoder.models import ToolResult, tool_failure
from tricoder.execution_state import EffectState, ErrorCode, FileEffects, RecoveryAction, ToolError
from tricoder.workspace.verification import stable_snapshots
from tricoder.policy import (
    CommandFormError,
    CommandPolicy,
    PolicyError,
    command_form_feedback,
)
from tricoder.process.control import ProcessExecutionUncertain, run_bounded_process
from tricoder.process.env import filtered_subprocess_env

from tricoder.tools.handlers import InvalidToolArgument, ToolHandler


_filtered_env = filtered_subprocess_env
_CHECK_OUTPUT_CHARS = 2_000
_ZERO_TESTS_PATTERN = re.compile(r"\bRan\s+0\s+tests?\b", re.IGNORECASE)


def _public_argv(args: list[str]) -> tuple[str, ...]:
    """隐藏解释器绝对路径，但保留实际、规范化的其余参数。"""

    executable = Path(args[0]).name.lower().removesuffix(".exe")
    if CommandPolicy._is_python_executable(args[0]):
        executable = "python"
    return (executable, *args[1:])


def _test_target(token: str) -> str:
    """把 unittest 的简单模块目标显示为可理解的相对文件名。"""

    if token.endswith(".py") or "/" in token or "\\" in token:
        return token.replace("\\", "/")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", token):
        return token.replace(".", "/") + ".py"
    return token


def _check_kind_and_targets(
    args: list[str], *, information_command: bool
) -> tuple[str, tuple[str, ...]]:
    """只按策略已验证的 argv 分类，不从 stdout 或模型说明推断。"""

    if information_command:
        return "information", ()
    if not CommandPolicy._is_python_executable(args[0]):
        return "other", ()
    if len(args) >= 2 and args[1].endswith(".py"):
        return "script", (args[1].replace("\\", "/"),)
    if len(args) < 3 or args[1] != "-m":
        return "other", ()

    module = args[2].lower()
    tokens = args[3:]
    if module == "unittest":
        targets: list[str] = []
        discover = bool(tokens and tokens[0].lower() == "discover")
        index = 0
        while index < len(tokens):
            token = tokens[index]
            name, separator, value = token.partition("=")
            if name in CommandPolicy._UNITTEST_VALUE_OPTIONS:
                if not separator and index + 1 < len(tokens):
                    value = tokens[index + 1]
                    index += 1
                if name in {"-s", "--start-directory"} and value:
                    targets.append(value.replace("\\", "/"))
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            if token != "discover":
                targets.append(_test_target(token))
            index += 1
        if discover and not targets:
            targets.append(".")
        return "tests", tuple(dict.fromkeys(targets))
    if module == "pytest":
        value_options = {
            "--capture", "--maxfail", "-k", "-m", "--tb", "-r",
            "--durations", "--durations-min", "--ignore", "--deselect",
            "--ignore-glob", "--import-mode",
        }
        targets = _positional_targets(tokens, value_options=value_options)
        return "tests", tuple(dict.fromkeys(targets or ["."]))
    if module == "compileall":
        targets = _positional_targets(
            tokens,
            value_options={"-j", "-x", "-r", "--invalidation-mode"},
        )
        return "syntax", tuple(dict.fromkeys(targets))
    if module in {"ruff", "mypy"}:
        if module == "ruff" and tokens and tokens[0].lower() == "check":
            tokens = tokens[1:]
        value_options = (
            {
                "--select", "--ignore", "--extend-select", "--extend-ignore",
                "--per-file-ignores", "--output-format", "--target-version",
                "--line-length",
            }
            if module == "ruff"
            else {"--exclude", "--follow-imports", "--python-version", "--platform"}
        )
        targets = _positional_targets(tokens, value_options=value_options)
        return "static", tuple(dict.fromkeys(targets or ["."]))
    return "other", ()


def _positional_targets(
    tokens: list[str],
    *,
    value_options: set[str],
) -> list[str]:
    """按工具选项语法提取位置目标，绝不把选项值描述成已检查路径。"""

    targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, separator, _value = token.partition("=")
        if name in value_options:
            index += 1 if separator else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        targets.append(token.replace("\\", "/"))
        index += 1
    return targets


def _command_output_summary(
    stdout: str,
    stderr: str,
) -> tuple[str, bool, tuple[str, ...]]:
    """形成有界诊断摘要；输出文本没有签发验证结论的权限。"""

    output = f"stdout:\n{stdout}\nstderr:\n{stderr}"
    truncated = len(output) > _CHECK_OUTPUT_CHARS
    summary = output[:_CHECK_OUTPUT_CHARS]
    diagnostics = ("zero_tests_reported",) if _ZERO_TESTS_PATTERN.search(output) else ()
    return summary, truncated, diagnostics


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
    description = (
        "经审批后在工作区内运行白名单命令；Python 别名统一使用会话解释器，"
        "标准测试形式为 `python -m unittest discover -v`，版本查询仅用于诊断。"
        "请按当前任务选择相关检查；不要为了结束任务重复执行无关测试。"
    )
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
        return await run_in_cleanup_thread(
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
            return tool_failure(ErrorCode.POLICY_DENIED, "只读模式禁止执行命令")
        command = self._required_str(arguments, "command")
        cwd = self.context.workspace_policy.resolve_path(str(arguments.get("cwd", ".")))
        if not cwd.is_dir():
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "命令工作目录必须是目录")
        command_policy = self.context.command_policy.scoped_to(
            self.context.workspace_policy
        )
        try:
            args = command_policy.validate(command, cwd=cwd)
        except CommandFormError as exc:
            return tool_failure(
                ErrorCode.INVALID_ARGUMENT,
                command_form_feedback(exc.reason),
                recovery=RecoveryAction.REPLAN,
            )
        subprocess_env = command_policy.subprocess_environment()
        information_command = CommandPolicy.is_information_command(args)
        executable = Path(args[0]).name.lower().removesuffix(".exe")
        if executable == "git" and _git_command_escapes_workspace(
            cwd,
            command_policy,
        ):
            return tool_failure(
                ErrorCode.POLICY_DENIED,
                "git 仓库根超出工作区，拒绝执行（防止读取工作区外仓库内容）",
            )
        normalized_argv = json.dumps(args, ensure_ascii=False)
        interpreter_note = (
            "\n说明：Python 请求已归一化到当前会话解释器。"
            if CommandPolicy._is_python_executable(args[0])
            else ""
        )
        detail = (
            f"原始请求：{command}\n"
            f"归一化 argv：{normalized_argv}\n"
            f"有效目录：{cwd}\n"
            f"超时：{self.context.timeout:g} 秒"
            f"{interpreter_note}"
        )
        auto_approved = (
            self.context.auto_approve_git is not None
            and self.context.auto_approve_git(args)
        )
        approved = auto_approved or self.context.approver("run_command", detail)
        if not approved:
            return tool_failure(ErrorCode.APPROVAL_DENIED, "用户拒绝了命令执行")
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        scope = self.context.verification_scope
        verification_command = _is_verification_command(args)
        public_argv = _public_argv(args)
        relative_cwd = cwd.relative_to(
            self.context.workspace_policy.workspace
        ).as_posix()
        relative_cwd = relative_cwd if relative_cwd != "." else "."
        check_kind, check_targets = _check_kind_and_targets(
            args,
            information_command=information_command,
        )

        def command_check(
            *,
            returncode: int | None,
            stdout: str = "",
            stderr: str = "",
            after_snapshot=None,
            limitations: tuple[str, ...] = (),
            force_truncated: bool = False,
            execution_complete: bool = False,
            workspace_stable: bool = False,
        ):
            summary, truncated, diagnostics = _command_output_summary(stdout, stderr)
            return scope.issue_command_check(
                argv=public_argv,
                cwd=relative_cwd,
                kind=check_kind,
                returncode=returncode,
                output_summary=summary,
                execution_complete=execution_complete,
                workspace_stable=workspace_stable,
                targets=check_targets,
                snapshot_id=(
                    after_snapshot.digest
                    if after_snapshot is not None and after_snapshot.complete
                    else None
                ),
                limitations=limitations,
                diagnostics=diagnostics,
                output_truncated=truncated or force_truncated,
            )
        # 文件状态观察与验证证据签发是两条独立边界：普通脚本也必须通过
        # 前后快照证明受覆盖工作区稳定，但只有认可检查命令可签发证据。
        try:
            before = (
                scope.capture(self.context.workspace_policy)
                if not information_command
                else None
            )
        except OSError:
            return tool_failure(
                ErrorCode.RESULT_UNCERTAIN,
                "命令执行前文件状态观察失败，未启动命令",
                file_effects=FileEffects(EffectState.UNKNOWN),
            )
        if (
            before is not None
            and not verification_command
            and not before.complete
        ):
            return tool_failure(
                ErrorCode.RESULT_UNCERTAIN,
                "命令执行前文件状态观察不完整，未启动命令",
                file_effects=FileEffects(EffectState.UNKNOWN),
            )
        # 先记本地“可能已启动”，异常/取消不能沿旧证据恢复；仅稳定清理后的快照收窄。
        previous_unknown = scope.unknown_effects
        scope.unknown_effects = True
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
        except ProcessExecutionUncertain as exc:
            return tool_failure(
                (
                    ErrorCode.CLEANUP_FAILED
                    if exc.cleanup_failed
                    else ErrorCode.RESULT_UNCERTAIN
                ),
                "命令执行结果或进程树清理无法确认",
                command_check=command_check(
                    returncode=None,
                    limitations=("命令执行结果或清理状态无法确认",),
                ),
            )
        except OSError:
            scope.unknown_effects = previous_unknown
            return tool_failure(ErrorCode.EXECUTION_FAILED, "命令进程无法安全启动")
        if completed.cleanup_failed:
            return tool_failure(
                ErrorCode.CLEANUP_FAILED,
                "命令进程树清理失败，结果不可信",
                command_check=command_check(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    limitations=("进程树清理失败",),
                ),
            )
        if completed.timed_out:
            return tool_failure(
                ErrorCode.TIMEOUT,
                f"命令执行超过 {self.context.timeout:g} 秒",
                command_check=command_check(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    limitations=("命令超时，结果不完整",),
                ),
            )
        if completed.output_exceeded:
            captured = self._bounded(
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
            return tool_failure(
                ErrorCode.OUTPUT_LIMIT,
                f"命令输出超过 {self.context.max_output_chars} 字符，已终止进程树\n"
                f"{captured}",
                command_check=command_check(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    limitations=("命令输出超过限制，结果不完整",),
                    force_truncated=True,
                ),
            )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        try:
            after = (
                scope.capture(self.context.workspace_policy)
                if before is not None
                else None
            )
        except OSError:
            return tool_failure(
                ErrorCode.RESULT_UNCERTAIN,
                "命令已执行，但文件状态观察失败，影响无法确认",
                file_effects=FileEffects(EffectState.UNKNOWN),
                command_check=command_check(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    limitations=("执行后文件状态观察失败",),
                ),
            )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if (
            after is not None
            and not verification_command
            and not after.complete
        ):
            return tool_failure(
                ErrorCode.RESULT_UNCERTAIN,
                "命令已执行，但文件状态观察不完整，影响无法确认",
                file_effects=FileEffects(EffectState.UNKNOWN),
                command_check=command_check(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    after_snapshot=after,
                    limitations=("执行后文件状态观察不完整", *after.limitations),
                ),
            )
        stable = before is not None and after is not None and stable_snapshots(before, after)
        succeeded = completed.returncode == 0
        information_stable = information_command and succeeded
        if stable or information_stable:
            scope.unknown_effects = previous_unknown
        evidence = (
            scope.issue(before, after, completed.returncode == 0)
            if verification_command and before is not None and after is not None
            else None
        )
        output = (
            f"退出码：{completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        check_limitations: tuple[str, ...]
        if information_command:
            check_limitations = ("版本查询不检查工作区文件状态",)
        elif before is None or after is None:
            check_limitations = ("缺少完整的工作区状态观察",)
        elif not before.complete or not after.complete:
            check_limitations = (
                "工作区状态观察不完整",
                *before.limitations,
                *after.limitations,
            )
        elif not stable:
            check_limitations = ("命令执行后工作区状态发生变化",)
        else:
            check_limitations = ()
        return ToolResult(
            succeeded,
            self._bounded(output),
            verification_passed=(
                succeeded if verification_command else None
            ),
            error=None if succeeded else ToolError(ErrorCode.EXECUTION_FAILED, RecoveryAction.REPLAN),
            file_effects=FileEffects(
                EffectState.NONE
                if stable or information_stable
                else EffectState.UNKNOWN
            ),
            verification_evidence=evidence,
            command_check=command_check(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                after_snapshot=after,
                limitations=check_limitations,
                execution_complete=True,
                workspace_stable=stable,
            ),
        )


def _is_verification_command(args: list[str]) -> bool:
    """只有认可的测试/编译/静态检查命令才产生验证结论。

    git 只读命令与普通脚本执行不改变验证状态。
    """
    return CommandPolicy.is_verification_command(args)


class GitDiffTool(ToolHandler):
    """只读展示工作区未提交变更的 diff 统计，无需审批（策略已限只读）。"""

    name = "git_diff"
    description = "显示工作区未提交变更的只读 diff 统计。"
    parameters = ToolHandler._schema({})

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = self.context.workspace_policy.workspace
        if _git_command_escapes_workspace(workspace, self.context.command_policy):
            return tool_failure(
                ErrorCode.POLICY_DENIED,
                "git 仓库根超出工作区，拒绝执行（防止读取工作区外仓库内容）",
            )
        try:
            args = self.context.command_policy.validate("git --no-pager diff --stat")
        except PolicyError:
            return tool_failure(ErrorCode.POLICY_DENIED, "本地命令策略拒绝操作")
        try:
            completed = run_bounded_process(
                args,
                cwd=workspace,
                env=self.context.command_policy.subprocess_environment(),
                timeout=self.context.timeout,
                max_output_bytes=self.context.max_output_chars,
            )
        except ProcessExecutionUncertain as exc:
            return tool_failure(ErrorCode.CLEANUP_FAILED if exc.cleanup_failed else ErrorCode.RESULT_UNCERTAIN,
                                "git diff 执行结果或进程树清理无法确认")
        except OSError:
            return tool_failure(ErrorCode.EXECUTION_FAILED, "git diff 进程无法安全启动")
        if completed.cleanup_failed:
            return tool_failure(ErrorCode.CLEANUP_FAILED, "git diff 进程树清理失败，结果不可信")
        if completed.timed_out:
            return tool_failure(ErrorCode.TIMEOUT, f"git diff 超过 {self.context.timeout:g} 秒")
        if completed.output_exceeded:
            return tool_failure(ErrorCode.OUTPUT_LIMIT, "git diff 输出超过限制，已终止进程树")
        output = (completed.stdout or completed.stderr).strip()
        if not output:
            output = "工作区没有未提交变更"
        return ToolResult(completed.returncode == 0, self._bounded(output),
                          error=None if completed.returncode == 0 else
                          ToolError(ErrorCode.EXECUTION_FAILED, RecoveryAction.REPLAN))


class FinishTool(ToolHandler):
    name = "finish"
    description = (
        "结束本轮任务并提交文字总结；完成、无法继续或需要用户补充信息时必须调用。"
        "outcome=completed 表示模型声明请求已交付，outcome=incomplete 表示仍未完成；"
        "省略 outcome 兼容为 completed。本工具只请求结束，本地验证、检查事实和最终安全状态仍由宿主决定。"
    )
    parameters = ToolHandler._schema(
        {
            "summary": {"type": "string"},
            "outcome": {
                "type": "string",
                "enum": ["completed", "incomplete"],
            },
        },
        ["summary"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        summary = self._required_str(arguments, "summary")
        outcome = arguments.get("outcome", "completed")
        if outcome not in {"completed", "incomplete"}:
            raise InvalidToolArgument("outcome 只能是 completed 或 incomplete")
        return ToolResult(True, summary)
