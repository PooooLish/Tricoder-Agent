"""TriCoder 命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, TextIO

from rich.console import Console

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.config import ConfigError, load_config, provider_key_env
from tricoder.models import ProviderConfig
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ModelProvider, OpenAICompatibleProvider
from tricoder.session_runtime import RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.sessions import SessionError, SessionStore, default_sessions_db
from tricoder.shell import InteractiveShell
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.ui import TerminalUI


ProviderFactory = Callable[[ProviderConfig, float], ModelProvider]
SessionStoreFactory = Callable[[Path], SessionStore]
ShellFactory = Callable[..., InteractiveShell]


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


def _default_provider_factory(config: ProviderConfig, timeout: float) -> ModelProvider:
    return OpenAICompatibleProvider(config, timeout=timeout)


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
    )

    doctor = subparsers.add_parser("doctor", help="检查本地配置，不发送 API 请求")
    _add_common_options(doctor)

    run = subparsers.add_parser("run", help="在指定工作区运行 Coding Agent")
    run.add_argument("task", help="希望 Agent 完成的编码任务")
    _add_common_options(run)
    chat = subparsers.add_parser("chat", help="进入持续交互会话")
    _add_chat_options(chat)
    run.add_argument("--max-rounds", type=int, help="最大模型调用轮数")
    run.add_argument("--max-context-chars", type=int, help="模型消息上下文最大字符数")
    run.add_argument("--timeout", type=float, help="API 与命令超时秒数")
    run.add_argument("--read-only", action="store_true", help="禁止编辑文件和执行命令")
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


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    provider_factory: ProviderFactory = _default_provider_factory,
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
        )
    except ConfigError as exc:
        ui.show_error("配置错误", str(exc))
        return 2

    if args.command == "doctor":
        key_name = provider_key_env(args.provider)
        ui.show_doctor(config, key_name)
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if config.audit_dir is None:
        raise RuntimeError("运行配置缺少审计目录")
    audit_path = config.audit_dir / f"{run_id}.jsonl"
    audit = AuditLogger(audit_path)
    try:
        audit.prepare()
    except OSError:
        ui.show_error("配置错误", "无法准备可写的审计日志")
        return 2

    ui.show_start(args.task, config)
    provider = provider_factory(config.provider, config.timeout)
    tools = ToolRegistry(
        ToolContext(
            workspace_policy=WorkspacePolicy(config.workspace),
            command_policy=CommandPolicy(),
            approver=ui.approve,
            read_only=config.read_only,
            timeout=config.timeout,
        )
    )
    agent = CodingAgent(
        provider,
        tools,
        max_rounds=config.max_rounds,
        max_context_chars=config.max_context_chars,
        audit=audit,
        observer=ui,
    )
    result = agent.run(args.task)
    ui.show_complete(result, audit_path)
    return 0 if result.ok else 1


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
            ),
            provider_factory=provider_factory,
            approver=ui.approve,
            observer=ui,
        )
    except (ConfigError, SessionError, SessionRuntimeError, OSError, ValueError):
        # 不显示底层异常，避免路径或损坏细节进入交互终端输出。
        ui.show_error("配置错误", "无法安全初始化交互会话")
        return 2

    shell = shell_factory(runtime, ui, input_fn=input_fn)
    return shell.run()
