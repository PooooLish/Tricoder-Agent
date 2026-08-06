"""基于 Textual 的本地交互 TUI。

完全复用 SessionRuntime / CodingAgent / ToolRegistry / CommandPolicy 的安全
边界：写操作与命令执行仍需在模态审批中明确确认；read_only 由运行配置决定。
Agent 循环在后台线程运行，UI 事件通过 Textual 线程安全机制转发。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, RichLog, Static

from tricoder.agent import AgentObserver
from tricoder.commands import CommandError, is_slash_command, list_commands, parse_command
from tricoder.models import RunResult, TokenUsage, ToolAction, ToolResult
from tricoder.session_runtime import SessionRuntime, SessionRuntimeError


def _format_token_usage(usage: TokenUsage) -> str:
    def count(value: int | None) -> str:
        return "-" if value is None else f"{value:,}"

    ratio = usage.cache_hit_ratio
    ratio_text = "-" if ratio is None else f"{ratio:.1%}"
    return (
        f"输入 {count(usage.input_tokens)} · "
        f"缓存 {count(usage.cached_tokens)} ({ratio_text}) · "
        f"输出 {count(usage.output_tokens)}"
    )


class ApprovalScreen(ModalScreen[bool]):
    """展示动作详情，仅接受明确的 y 允许 / n 或 Esc 拒绝。"""

    BINDINGS = [
        ("y", "approve", "允许"),
        ("n", "reject", "拒绝"),
        ("escape", "reject", "取消"),
    ]

    def __init__(self, action: str, detail: str) -> None:
        super().__init__()
        self._action = action
        self._detail = detail

    def compose(self) -> ComposeResult:
        yield Static(f"[bold yellow]待审批动作：{self._action}[/bold yellow]")
        yield Static(self._detail, classes="approval-detail")
        yield Static("[dim]按 y 允许，n 或 Esc 拒绝[/dim]", classes="approval-hint")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class TuiObserver(AgentObserver):
    """把后台线程中的 Agent 事件转发到 UI 线程。"""

    def __init__(self, app: "TricoderApp") -> None:
        self._app = app

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        self._app.log_line_safe(f"[dim]第 {round_number}/{max_rounds} 轮：请求模型…[/dim]")

    def on_action(self, action: ToolAction) -> None:
        self._app.log_line_safe(f"[bold cyan]● {action.tool}[/bold cyan]  {action.reason}")

    def on_tool_result(
        self,
        action: ToolAction,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        icon = "✓" if result.ok else "✗"
        color = "green" if result.ok else "red"
        self._app.log_line_safe(
            f"  [{color}]{icon}[/{color}] {len(result.output):,} 字符 · {duration_ms} ms"
        )

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
        self._app.log_line_safe(
            f"[cyan]第 {round_number} 轮用量 · {_format_token_usage(usage)}[/cyan]"
        )

    def on_error(self, message: str) -> None:
        self._app.log_line_safe(f"[red]✗ {message}[/red]")


class TricoderApp(App[None]):
    """TriCoder 本地交互界面。"""

    TITLE = "TriCoder TUI"
    SUB_TITLE = "安全审批式 Coding Agent"

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+c", "cancel", "清空输入"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #log {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #prompt {
        dock: bottom;
        height: 3;
    }
    ApprovalScreen {
        align: center middle;
        background: $surface;
        border: round $warning;
        padding: 1 2;
        width: 80%;
        height: auto;
    }
    .approval-detail {
        margin: 1 0;
        color: $text;
    }
    .approval-hint {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        runtime_factory: Callable[[AgentObserver, Callable[[str, str], bool]], SessionRuntime],
    ) -> None:
        super().__init__()
        self._runtime_factory = runtime_factory
        self.runtime: SessionRuntime | None = None
        self.last_result: RunResult | None = None
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield RichLog(id="log", wrap=True, highlight=True, markup=True)
        yield Input(id="prompt", placeholder="输入任务，或 /help 查看本地命令")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.runtime = self._runtime_factory(TuiObserver(self), self._approver)
        except Exception as exc:
            self.log_line(f"[red]无法安全初始化会话：{type(exc).__name__}[/red]")
            self.exit(2)
            return
        record = self.runtime.current.record
        self.log_line(
            f"[bold cyan]TriCoder[/bold cyan] · 会话 {record.name} · "
            f"{record.provider} · {record.model}"
        )
        self.log_line(f"[dim]工作区：{record.workspace}[/dim]")
        self.query_one(Input).focus()

    # ---- 线程安全日志 ----

    def log_line_safe(self, text: str) -> None:
        """后台线程调用；转发到 UI 线程。"""
        try:
            self.call_from_thread(self._log_line_impl, text)
        except Exception:
            pass

    def log_line(self, text: str) -> None:
        """UI 线程直接写入。"""
        self._log_line_impl(text)

    def _log_line_impl(self, text: str) -> None:
        self._lines.append(text)
        self.query_one("#log", RichLog).write(text)

    # ---- 任务与命令 ----

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        self.query_one(Input).value = ""
        if not text:
            return
        if is_slash_command(text):
            self._handle_command(text)
        else:
            self.log_line(f"[bold]任务：[/bold]{text}")
            self.run_worker(
                lambda: self._run_task(text), thread=True, exclusive=True,
                name="agent-task",
            )

    def _run_task(self, task: str) -> None:
        if self.runtime is None:
            return
        try:
            result = self.runtime.run_task(task)
        except SessionRuntimeError as exc:
            self.log_line_safe(f"[red]任务运行失败：{exc}[/red]")
            return
        except Exception as exc:  # 兜底：任何未预期异常都不能杀死 TUI
            self.log_line_safe(f"[red]任务异常：{type(exc).__name__}[/red]")
            return
        self._log_result(result)

    def _log_result(self, result: RunResult) -> None:
        self.last_result = result
        color = "green" if result.ok else "red"
        state = "完成" if result.ok else "未完成"
        lines = [
            f"[{color}]{state}[/{color}] · 工具调用 {result.tool_calls} · "
            f"修改文件 {len(result.modified_files)} · 验证 {result.verification}",
            f"[dim]{result.summary}[/dim]",
        ]
        if result.usage is not None:
            lines.append(f"[dim]累计用量 · {_format_token_usage(result.usage)}[/dim]")
        for line in lines:
            self.log_line_safe(line)

    # ---- 审批 ----

    def _approver(self, action: str, detail: str) -> bool:
        """后台线程调用；通过模态等待用户明确确认。"""
        response: dict[str, bool] = {}
        done = threading.Event()

        def request() -> None:
            async def ask() -> None:
                value = await self.push_screen_wait(ApprovalScreen(action, detail))
                response["ok"] = bool(value)
                done.set()

            self.run_worker(ask, thread=False, name="approval-wait")

        self.call_from_thread(request)
        done.wait()
        return response.get("ok", False)

    def _confirm(self, prompt: str) -> bool:
        return self._approver("确认", prompt)

    # ---- 斜杠命令 ----

    def _handle_command(self, text: str) -> None:
        try:
            command = parse_command(text)
        except CommandError as exc:
            self.log_line(f"[red]{exc}[/red]")
            return
        try:
            if command.name == "help":
                self._show_help()
            elif command.name == "status":
                self._show_status()
            elif command.name == "clear":
                self._clear_current()
            elif command.name == "diff":
                self._show_diff()
            elif command.name == "undo":
                self._undo()
            elif command.name == "session":
                self._handle_session(command.subcommand, command.argument)
            elif command.name == "exit":
                self.action_quit()
        except SessionRuntimeError as exc:
            self.log_line(f"[red]会话操作失败：{exc}[/red]")

    def _show_help(self) -> None:
        for name, spec in list_commands().items():
            self.log_line(f"[dim]/{name:<12}{spec.description}[/dim]")
        self.log_line("[dim]/session new <名称>     创建并切换到新会话[/dim]")

    def _show_status(self) -> None:
        if self.runtime is None:
            return
        record = self.runtime.current.record
        config = self.runtime.current.config
        memory = self.runtime.current.memory
        self.log_line(
            f"[cyan]会话[/cyan] {record.name} · [cyan]Provider[/cyan] "
            f"{record.provider} · [cyan]模型[/cyan] {record.model}"
        )
        self.log_line(
            f"[cyan]工作区[/cyan] {record.workspace} · "
            f"[cyan]模式[/cyan] {'只读' if config.read_only else '可编辑 · 人工审批'} · "
            f"[cyan]验证[/cyan] {memory.verification}"
        )

    def _clear_current(self) -> None:
        if not self._confirm("清除当前会话的上下文和摘要？"):
            self.log_line("[dim]已取消清除[/dim]")
            return
        self.run_worker(
            self._clear_worker, thread=True, exclusive=True, name="session-clear"
        )

    def _clear_worker(self) -> None:
        if self.runtime is not None:
            self.runtime.clear_current()
            self.log_line_safe("[yellow]当前会话记忆已清除[/yellow]")

    def _show_diff(self) -> None:
        if self.runtime is None:
            return
        latest = self.runtime.diff_latest()
        if latest is None:
            self.log_line("[dim]当前 Session 没有最近任务变更。[/dim]")
            return
        self.log_line("[bold]最近任务变更：[/bold]")
        for line in latest.splitlines():
            self.log_line(line)

    def _undo(self) -> None:
        if self.runtime is None:
            return
        try:
            preview = self.runtime.prepare_undo()
        except SessionRuntimeError as exc:
            self.log_line(f"[red]{exc}[/red]")
            return
        self.log_line("[bold]撤销预览：[/bold]")
        for line in preview.diff.splitlines():
            self.log_line(line)
        if not self._confirm("撤销最近一条任务的全部文件修改？"):
            self.log_line("[dim]已取消撤销[/dim]")
            return
        self.run_worker(self._undo_worker, thread=True, exclusive=True, name="session-undo")

    def _undo_worker(self) -> None:
        if self.runtime is None:
            return
        execution = self.runtime.undo_latest()
        if execution.ok:
            self.log_line_safe("[yellow]已撤销最近一条任务的全部文件修改。[/yellow]")
        elif execution.conflicts:
            self.log_line_safe(
                f"[red]撤销冲突，未执行：{'、'.join(execution.conflicts)}[/red]"
            )
        elif execution.compensation_failed:
            self.log_line_safe(
                f"[red]撤销未完成且补偿失败：{'、'.join(execution.compensation_failed)}[/red]"
            )
        else:
            self.log_line_safe("[red]撤销未完成，文件未被修改。[/red]")

    def _handle_session(self, subcommand: str | None, argument: str | None) -> None:
        if self.runtime is None:
            return
        if subcommand is None:
            self.log_line("[dim]跨工作区会话列表与切换暂未在 TUI 提供，请使用交互 CLI。[/dim]")
        elif subcommand == "current":
            self._show_status()
        elif subcommand == "new":
            self.run_worker(
                lambda: self._session_new_worker(argument or "default"),
                thread=True, exclusive=True, name="session-new",
            )

    def _session_new_worker(self, name: str) -> None:
        if self.runtime is None:
            return
        self.runtime.create(name)
        self.log_line_safe(f"[yellow]已创建并切换到新会话：{name}[/yellow]")

    # ---- 退出 ----

    def action_cancel(self) -> None:
        self.query_one(Input).value = ""

    def action_quit(self) -> None:
        code = 1
        if self.runtime is not None:
            code = 0 if self.runtime.retry_persist() else 1
        self.exit(code)
