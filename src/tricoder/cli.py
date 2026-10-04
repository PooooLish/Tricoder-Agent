"""TriCoder 命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, TextIO

from rich.console import Console

from tricoder.agent import AgentObserver, CodingAgent
from tricoder.audit import AuditLogger
from tricoder.changes import ChangeJournal, TaskChangeSet
from tricoder.config import ConfigError, load_config, provider_key_env
from tricoder.context.spill import SpillError, ToolResultSpillStore
from tricoder.core.cancellation import CancellationToken
from tricoder.evals.service import run_eval_command
from tricoder.mcp.sdk import MCPDependencyError
from tricoder.models import AppConfig, ProviderConfig, RunResult, SessionContext
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ModelProvider, create_provider
from tricoder.session.runtime import (
    RuntimeOptions,
    SessionInUseError,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.session.store import SessionError, SessionStore, default_sessions_db
from tricoder.presentation.shell import InteractiveShell
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.presentation.console import TerminalUI
from tricoder.workspace.gate import WorkspaceGatePreview
from tricoder.workspace.lock import (
    WorkspaceIdentityError,
    WorkspaceLock,
    WorkspaceLockBusyError,
    WorkspaceLockError,
    WorkspaceRecoveryRequiredError,
)
from tricoder.workspace.snapshot import (
    SnapshotLimits,
    WorkspaceBaseline,
    WorkspaceScanError,
    capture_workspace_baseline,
    task_changes_match_baselines,
)


ProviderFactory = Callable[[ProviderConfig, float], ModelProvider]
SessionStoreFactory = Callable[[Path], SessionStore]
ShellFactory = Callable[..., InteractiveShell]
MCP_SDK_MISSING_GUIDANCE = "MCP SDK 未安装；请按 requirements.lock 安装项目锁定依赖"


class ConsoleApprover:
    """在终端展示动作详情，只接受明确的肯定回答。"""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output: TextIO = sys.stdout,
    ) -> None:
        self.input_fn = input_fn
        self.output = output

    def __call__(self, action: str, detail: str) -> bool:
        self.output.write(f"\n待审批动作：{action}\n{detail}\n")
        self.output.flush()
        answer = self.input_fn("允许执行？[y/N] ").strip().lower()
        return answer in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tricoder",
        description="支持 OpenAI、DeepSeek、GLM 的安全审批式 Coding Agent",
    )
    # 裸 `tricoder` 需要进入交互模式，因此子命令不能设为必填。
    subparsers = parser.add_subparsers(dest="command")
    # 根解析器也需要完整的 chat 默认值，供完全不带参数的交互入口使用。
    parser.set_defaults(
        provider=None,
        workspace=Path.cwd(),
        model=None,
        base_url=None,
        env_file=None,
        audit_dir=None,
        no_color=False,
        max_rounds=None,
        max_context_chars=None,
        timeout=None,
        read_only=False,
        no_plan=False,
    )

    doctor = subparsers.add_parser("doctor", help="检查本地配置，不发送 API 请求")
    _add_common_options(doctor)

    run = subparsers.add_parser("run", help="在指定工作区运行 Coding Agent")
    run.add_argument("task", help="希望 Agent 完成的编码任务")
    _add_common_options(run)
    chat = subparsers.add_parser("chat", help="进入持续交互会话")
    _add_chat_options(chat)
    tui = subparsers.add_parser("tui", help="进入 Textual 交互 TUI")
    _add_chat_options(tui)
    eval_command = subparsers.add_parser("eval", help="运行本地 Coding Agent 评测")
    eval_command.add_argument("suite", nargs="?", type=Path, help="评测套件目录")
    eval_command.add_argument(
        "--experiment",
        type=Path,
        help="运行严格校验的实验清单；不能与 suite/--case/--repeat 组合",
    )
    eval_command.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="单条件套件重复次数，默认 1、上限 10",
    )
    eval_command.add_argument(
        "--provider",
        choices=("openai", "deepseek", "glm"),
        default="openai",
        help="模型服务，默认 openai",
    )
    eval_command.add_argument("--model", help="覆盖模型名称")
    eval_command.add_argument("--base-url", help="覆盖 OpenAI-compatible API 地址")
    eval_command.add_argument(
        "--env-file",
        type=Path,
        help="显式指定本地密钥文件",
    )
    eval_command.add_argument("--case", help="仅运行指定 case")
    eval_command.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验评测定义，不读取配置或创建运行状态",
    )
    eval_command.add_argument(
        "--no-color",
        action="store_true",
        help="关闭颜色，适合 CI 或重定向输出",
    )
    eval_compare = subparsers.add_parser(
        "eval-compare", help="离线比较两个 Eval v2 报告"
    )
    eval_compare.add_argument("baseline", type=Path, help="基线 result.json")
    eval_compare.add_argument("candidate", type=Path, help="候选 result.json")
    eval_compare.add_argument(
        "--allow-variable",
        action="append",
        choices=("model", "provider", "memory", "faults", "code"),
        default=[],
        help="声明允许变化的实验变量，可重复指定",
    )
    eval_compare.add_argument("--no-color", action="store_true", help="关闭颜色")
    run.add_argument("--max-rounds", type=int, help="最大模型调用轮数")
    run.add_argument("--max-context-chars", type=int, help="模型消息上下文最大字符数")
    run.add_argument("--timeout", type=float, help="API 与命令超时秒数")
    run.add_argument("--read-only", action="store_true", help="禁止编辑文件和执行命令")
    run.add_argument("--no-plan", action="store_true", help="跳过执行前的规划阶段")
    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("openai", "deepseek", "glm"),
        default="openai",
        help="模型服务，默认 openai",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Agent 可访问的工作区，默认当前目录",
    )
    parser.add_argument("--model", help="覆盖模型名称")
    parser.add_argument("--base-url", help="覆盖 OpenAI-compatible API 地址")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="显式指定本地密钥文件，可位于目标工作区之外",
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        help="审计日志目录；相对路径按当前进程目录解析",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="关闭颜色和动画，适合 CI 或重定向输出",
    )


def _add_chat_options(parser: argparse.ArgumentParser) -> None:
    """添加 chat 的完整运行参数；与 run 相同，但不包含 task 位置参数。"""

    _add_common_options(parser)
    # None 表示用户未显式指定 Provider，恢复会话时不得覆盖原有选择。
    parser.set_defaults(provider=None)
    parser.add_argument("--max-rounds", type=int, help="最大模型调用轮数")
    parser.add_argument("--max-context-chars", type=int, help="模型消息上下文最大字符数")
    parser.add_argument("--timeout", type=float, help="API 与命令超时秒数")
    parser.add_argument("--read-only", action="store_true", help="禁止编辑文件和执行命令")
    parser.add_argument("--no-plan", action="store_true", help="跳过执行前的规划阶段")


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    provider_factory: ProviderFactory = create_provider,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    session_store_factory: SessionStoreFactory = SessionStore,
    shell_factory: ShellFactory = InteractiveShell,
) -> int:
    """执行 CLI，并以稳定退出码向脚本报告结果。"""

    args = build_parser().parse_args(argv)
    console = Console(
        file=output,
        no_color=args.no_color,
        color_system=None if args.no_color else "auto",
    )
    ui = TerminalUI(console=console, input_fn=input_fn)
    env = os.environ if environ is None else environ
    if args.command == "eval":
        try:
            return run_eval_command(
                args,
                environ=env,
                provider_factory=provider_factory,
                output=output,
            )
        except Exception:
            output.write("eval_error=runtime\n")
            return 2
    if args.command == "eval-compare":
        from tricoder.evals.compare import run_compare_command

        return run_compare_command(args, output=output)
    if args.command is None or args.command == "chat":
        return _run_chat(
            args,
            environ=env,
            provider_factory=provider_factory,
            ui=ui,
            input_fn=input_fn,
            session_store_factory=session_store_factory,
            shell_factory=shell_factory,
        )
    if args.command == "tui":
        return _run_tui(
            args,
            environ=env,
            provider_factory=provider_factory,
            session_store_factory=session_store_factory,
        )
    try:
        config = load_config(
            provider=args.provider,
            workspace=args.workspace,
            environ=env,
            env_file=args.env_file,
            audit_dir=args.audit_dir,
            model=args.model,
            base_url=args.base_url,
            max_rounds=getattr(args, "max_rounds", None),
            max_context_chars=getattr(args, "max_context_chars", None),
            timeout=getattr(args, "timeout", None),
            read_only=getattr(args, "read_only", False),
            plan_enabled=False if getattr(args, "no_plan", False) else None,
        )
    except ConfigError as exc:
        ui.show_error("配置错误", str(exc).replace("tool_protocol", "工具协议"))
        return 2

    if args.command == "doctor":
        key_name = provider_key_env(args.provider)
        ui.show_doctor(config, key_name)
        console.print(f"工具协议：{config.tool_protocol}")
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if config.audit_dir is None:
        raise RuntimeError("运行配置缺少审计目录")
    audit_path = config.audit_dir / f"{run_id}.jsonl"
    audit = AuditLogger(audit_path)

    return _run_one_shot_entry(
        args.task,
        config,
        source_env=env,
        provider_factory=provider_factory,
        ui=ui,
        audit=audit,
        audit_path=audit_path,
        run_id=run_id,
    )


def _run_one_shot_entry(
    task: str,
    config: AppConfig,
    *,
    source_env: Mapping[str, str],
    provider_factory: ProviderFactory,
    ui: TerminalUI,
    audit: AuditLogger,
    audit_path: Path,
    run_id: str,
) -> int:
    """在工作区锁和完整扫描内执行单次任务；失败绝不降级到无锁路径。"""

    try:
        ownership = WorkspaceLock.acquire(config.workspace)
    except WorkspaceLockBusyError:
        ui.show_error("工作区被占用", "同一工作区已有任务正在执行")
        return 2
    except WorkspaceRecoveryRequiredError:
        ui.show_error("工作区待恢复", "上次任务资源清理尚未确认，请先完成恢复检查")
        return 2
    except (WorkspaceIdentityError, WorkspaceLockError, OSError):
        ui.show_error("工作区错误", "无法安全取得工作区任务锁")
        return 2

    cancellation = CancellationToken()
    spill_store: ToolResultSpillStore | None = None
    active_marker = False
    spill_cleanup_failed = False
    marker_cleanup_failed = False
    result: RunResult | None = None
    exit_override: int | None = None
    journal = ChangeJournal()
    sealed: TaskChangeSet | None = None
    baseline: WorkspaceBaseline | None = None
    try:
        try:
            audit.prepare()
        except OSError:
            ui.show_error("配置错误", "无法准备可写的审计日志")
            return 2
        if _mcp_effectively_enabled(config):
            try:
                spill_store = ToolResultSpillStore(
                    config.audit_dir / "runtime" / "tool-results", run_id,
                )
            except SpillError:
                ui.show_error("配置错误", "无法准备安全的大型工具结果暂存目录")
                return 2
        try:
            baseline = capture_workspace_baseline(
                config.workspace,
                SnapshotLimits(),
                cancellation=cancellation,
            )
        except WorkspaceScanError as exc:
            ui.show_error("工作区扫描失败", f"扫描未完成（{exc.reason}），任务未启动")
            return 2
        try:
            ownership.mark_active()
            active_marker = True
        except (WorkspaceIdentityError, WorkspaceLockError, WorkspaceRecoveryRequiredError):
            ui.show_error("工作区错误", "无法安全建立任务活动标记")
            return 2

        ui.show_start(task, config)
        provider = provider_factory(config.provider, config.timeout)
        journal.begin_task((), "未运行")
        tools = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(config.workspace),
                command_policy=CommandPolicy(config.workspace),
                approver=ui.approve,
                read_only=config.read_only,
                timeout=config.timeout,
                change_journal=journal,
                spill_store=spill_store,
            )
        )
        agent = CodingAgent(
            provider,
            tools,
            max_rounds=config.max_rounds,
            max_context_chars=config.max_context_chars,
            audit=audit,
            observer=ui,
            tool_protocol=config.tool_protocol,
        )
        try:
            result = _run_once_with_config(
                config,
                agent,
                tools,
                task,
                source_env=source_env,
                audit=audit,
                cancellation=cancellation,
                event_sink=ui,
            )
            sealed = journal.seal_task(result.modified_files, result.verification)
        except MCPDependencyError:
            cancellation.cancel()
            ui.finish_stream()
            ui.show_error("配置错误", MCP_SDK_MISSING_GUIDANCE)
            exit_override = 2
        except KeyboardInterrupt:
            cancellation.cancel()
            ui.finish_stream()
            ui.show_error("任务已取消", "已停止当前任务")
            exit_override = 130
    finally:
        if spill_store is not None:
            try:
                spill_store.cleanup()
            except SpillError:
                spill_cleanup_failed = True
                ui.show_error("清理失败", "大型工具结果暂存未能安全清理")
        if active_marker and not spill_cleanup_failed:
            if result is not None and baseline is not None:
                result = _finalize_one_shot_workspace(
                    config,
                    baseline,
                    result,
                    sealed,
                    audit_path=audit_path,
                )
            else:
                # 主异常/取消仍做一次有界收尾扫描，但不以它建立任何可信基线。
                try:
                    capture_workspace_baseline(
                        config.workspace,
                        SnapshotLimits(),
                        cancellation=None,
                    )
                except WorkspaceScanError:
                    pass
            try:
                ownership.clear_active()
            except (WorkspaceIdentityError, WorkspaceLockError, OSError):
                marker_cleanup_failed = True
                ui.show_error("清理失败", "工作区活动标记未能安全清理")
        ownership.close()

    if spill_cleanup_failed or marker_cleanup_failed:
        return 1
    if exit_override is not None:
        return exit_override
    if result is None:
        return 1
    ui.show_complete(result, audit_path)
    return 0 if result.ok else 1


def _finalize_one_shot_workspace(
    config: AppConfig,
    baseline: WorkspaceBaseline,
    result: RunResult,
    change_set: TaskChangeSet | None,
    *,
    audit_path: Path,
) -> RunResult:
    """清理完成后复扫；只接受内置账本和本次精确审计文件能解释的变化。"""

    try:
        current = capture_workspace_baseline(
            config.workspace,
            SnapshotLimits(),
            # 任务取消后仍必须完成一致性复扫；否则已取消的令牌会让清理阶段
            # 直接失败，无法判断是否存在晚到写入或未知外部修改。
            cancellation=None,
        )
    except WorkspaceScanError:
        return replace(
            result,
            ok=False,
            summary=f"{result.summary}；工作区收尾扫描失败，当前代码状态待重新确认",
            verification="待验证",
        )
    framework_paths: tuple[str, ...] = ()
    tainted = False
    if change_set is not None:
        tainted = bool(change_set.tainted_paths)
    try:
        relative_audit = audit_path.resolve(strict=True).relative_to(config.workspace).as_posix()
    except (OSError, ValueError):
        pass
    else:
        framework_paths = (relative_audit,)
    try:
        attributable = task_changes_match_baselines(
            baseline,
            current,
            change_set,
            framework_owned_paths=framework_paths,
        )
    except (TypeError, ValueError):
        attributable = False
    if (
        result.ok
        and attributable
        and not tainted
        and not result.unknown_effects
        and not result.cleanup_failed
    ):
        return result
    if (
        change_set is None
        and attributable
        and not result.unknown_effects
        and not result.cleanup_failed
    ):
        return result
    return replace(
        result,
        ok=False,
        summary=f"{result.summary}；收尾发现未归属的工作区变化",
        verification="待验证",
    )


def _mcp_effectively_enabled(config: AppConfig) -> bool:
    """避免总开关开启但没有有效 server 时改变旧执行路径。"""

    return (
        config.extensions.enabled
        and config.mcp.enabled
        and any(server.enabled for server in config.mcp.servers)
    )


def _run_once_with_config(
    config: AppConfig,
    agent: CodingAgent,
    tools: ToolRegistry,
    task: str,
    *,
    source_env: Mapping[str, str],
    audit: AuditLogger | None,
    cancellation: CancellationToken,
    event_sink: AgentObserver | None,
    mcp_manager_factory: Callable[..., object] | None = None,
) -> RunResult:
    """只装配一次性任务执行；MCP 与交互运行时共用同一作用域。"""

    if not _mcp_effectively_enabled(config):
        return agent.run(
            task,
            cancellation=cancellation,
            event_sink=event_sink,
        )

    async def operation(_active_tools: ToolRegistry) -> RunResult:
        turn = await agent.run_with_context_async(
            task,
            SessionContext(),
            cancellation=cancellation,
            event_sink=event_sink,
        )
        return turn.result

    from tricoder.mcp.runtime import run_mcp_task_sync

    scope_kwargs: dict[str, object] = {}
    if mcp_manager_factory is not None:
        scope_kwargs["manager_factory"] = mcp_manager_factory
    return run_mcp_task_sync(
        config,
        tools,
        source_env=source_env,
        audit=audit,
        cancellation=cancellation,
        operation=operation,
        **scope_kwargs,
    )


def _run_chat(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    provider_factory: ProviderFactory,
    ui: TerminalUI,
    input_fn: Callable[[str], str],
    session_store_factory: SessionStoreFactory,
    shell_factory: ShellFactory,
) -> int:
    """装配交互会话；SQLite 或配置异常统一以稳定的配置错误退出。"""

    try:
        store = session_store_factory(default_sessions_db(environ))
        runtime = SessionRuntime(
            store,
            args.workspace,
            options=RuntimeOptions(
                environ=environ,
                env_file=args.env_file,
                audit_dir=args.audit_dir,
                provider=args.provider,
                model=args.model,
                base_url=args.base_url,
                max_rounds=args.max_rounds,
                max_context_chars=args.max_context_chars,
                timeout=args.timeout,
                read_only=args.read_only,
                plan_enabled=False if args.no_plan else None,
            ),
            provider_factory=provider_factory,
            approver=ui.approve,
            observer=ui,
            workspace_confirmer=ui.confirm_workspace_change,
        )
    except SessionInUseError as exc:
        ui.show_error("会话被占用", str(exc))
        return 2
    except (ConfigError, SessionError, SessionRuntimeError, OSError, ValueError):
        # 不显示底层异常，避免路径或损坏细节进入交互终端输出。
        ui.show_error("配置错误", "无法安全初始化交互会话")
        return 2

    try:
        shell = shell_factory(runtime, ui, input_fn=input_fn)
        code = shell.run()
    finally:
        closed = runtime.close()
    return code if closed else 1


def _run_tui(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    provider_factory: ProviderFactory,
    session_store_factory: SessionStoreFactory,
) -> int:
    """装配 Textual TUI；SQLite 或配置异常以稳定退出码结束。"""

    # textual 是可选交互依赖，仅在此入口延迟导入，避免普通 CLI 被其影响。
    from tricoder.presentation.tui import TricoderApp

    try:
        store = session_store_factory(default_sessions_db(environ))
    except (SessionError, OSError, ValueError):
        return 2

    def runtime_factory(
        observer: AgentObserver,
        approver: Callable[[str, str], bool],
    ) -> SessionRuntime:
        def confirm_workspace(preview: WorkspaceGatePreview) -> bool:
            if not preview.pages:
                return approver("初始化工作区基线", preview.message)
            total = len(preview.pages)
            for index, page in enumerate(preview.pages, start=1):
                detail = f"{preview.message}\n\n第 {index}/{total} 页：\n{page}"
                if not approver("确认任务前工作区差异", detail):
                    return False
            return True

        return SessionRuntime(
            store,
            args.workspace,
            options=RuntimeOptions(
                environ=environ,
                env_file=args.env_file,
                audit_dir=args.audit_dir,
                provider=args.provider,
                model=args.model,
                base_url=args.base_url,
                max_rounds=args.max_rounds,
                max_context_chars=args.max_context_chars,
                timeout=args.timeout,
                read_only=args.read_only,
                plan_enabled=False if args.no_plan else None,
            ),
            provider_factory=provider_factory,
            approver=approver,
            observer=observer,
            workspace_confirmer=confirm_workspace,
        )

    return TricoderApp(runtime_factory).run()
