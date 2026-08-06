"""基于 Textual 的本地交互 TUI。

完全复用 SessionRuntime / CodingAgent / ToolRegistry / CommandPolicy 的安全
边界：写操作与命令执行仍需在模态审批中明确确认；read_only 由运行配置决定。
Agent 循环在后台线程运行，UI 事件通过 Textual 线程安全机制转发。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from tricoder.agent import AgentObserver
from tricoder.commands import CommandError, is_slash_command, list_commands, parse_command
from tricoder.models import RunResult, SessionRecord, TokenUsage, ToolAction, ToolResult
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


class OptionListScreen(ModalScreen[str]):
    """通用方向键选择列表；Enter 确认、Esc 取消，返回选项值或 None。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(
        self,
        title: str,
        options: Sequence[str],
        current: str | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._current = current

    def compose(self) -> ComposeResult:
        yield Static(f"[bold cyan]{self._title}[/bold cyan]")
        items = [ListItem(Label(str(option))) for option in self._options]
        yield ListView(*items, id="options")
        yield Static("[dim]↑/↓ 选择 · Enter 确认 · Esc 取消[/dim]", classes="approval-hint")

    def on_mount(self) -> None:
        list_view = self.query_one("#options", ListView)
        if self._current is not None and self._current in self._options:
            list_view.index = self._options.index(self._current)
        list_view.focus()

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        event.stop()
        list_view = self.query_one("#options", ListView)
        self.dismiss(self._options[list_view.index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class TuiObserver(AgentObserver):
    """把后台线程中的 Agent 事件转发到 UI 线程。"""

    def __init__(self, app: "TricoderApp") -> None:
        self._app = app

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        self._app.begin_round(round_number, max_rounds)

    def on_action(self, action: ToolAction) -> None:
        self._app.round_line(f"[bold cyan]● {action.tool}[/bold cyan]  {action.reason}")

    def on_tool_result(
        self,
        action: ToolAction,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        icon = "✓" if result.ok else "✗"
        color = "green" if result.ok else "red"
        self._app.round_line(
            f"  [{color}]{icon}[/{color}] {len(result.output):,} 字符 · {duration_ms} ms"
        )
        self._app.round_summary(f"{action.tool} {icon} · {duration_ms} ms")

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
        self._app.round_line(
            f"[cyan]用量 · {_format_token_usage(usage)}[/cyan]"
        )

    def on_error(self, message: str) -> None:
        self._app.round_line(f"[red]✗ {message}[/red]")


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
    #main {
        height: 1fr;
    }
    #log {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #log Collapsible {
        margin: 0 0 1 0;
    }
    .log-line {
        margin: 0 0 1 0;
    }
    #sidebar {
        width: 30;
        border: round $primary;
        padding: 1;
        background: $panel;
    }
    #sidebar-content {
        color: $text;
        height: 1fr;
    }
    #prompt-bar {
        dock: bottom;
        height: auto;
        align: center middle;
        padding: 0 0 1 0;
    }
    #prompt {
        width: 100%;
        max-width: 110;
        height: 4;
    }
    #prompt:focus {
        border: round $accent;
    }
    ApprovalScreen {
        align: center middle;
        background: $surface;
        border: round $warning;
        padding: 1 2;
        width: 80%;
        max-width: 110;
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
        self._round_widgets: list[Collapsible] = []
        self._current_round_log: RichLog | None = None
        self._current_round_summary: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            VerticalScroll(id="log"),
            Vertical(Static("", id="sidebar-content"), id="sidebar", classes="sidebar"),
            id="main",
        )
        yield Horizontal(
            Input(id="prompt", placeholder="输入任务，或 /help 查看本地命令"),
            id="prompt-bar",
        )
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
        self._refresh_sidebar_impl()
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
        self.query_one("#log", VerticalScroll).mount(
            Static(text, classes="log-line")
        )
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def refresh_sidebar(self) -> None:
        """后台线程调用；刷新侧边状态栏。"""
        try:
            self.call_from_thread(self._refresh_sidebar_impl)
        except Exception:
            pass

    def _refresh_sidebar_impl(self) -> None:
        if self.runtime is None:
            return
        record = self.runtime.current.record
        config = self.runtime.current.config
        memory = self.runtime.current.memory
        workspace = str(record.workspace)
        if len(workspace) > 26:
            workspace = "…" + workspace[-25:]
        content = (
            "[b]会话[/b]\n"
            f"{record.name}\n\n"
            "[b]Provider[/b]\n"
            f"{record.provider}\n\n"
            "[b]模型[/b]\n"
            f"{record.model}\n\n"
            "[b]工作区[/b]\n"
            f"{workspace}\n\n"
            "[b]模式[/b]\n"
            f"{'只读' if config.read_only else '可编辑'}\n\n"
            "[b]权限[/b]\n"
            f"{self.runtime.permission_level}\n\n"
            "[b]验证[/b]\n"
            f"{memory.verification}\n\n"
            "[b]修改文件[/b]\n"
            f"{len(memory.modified_files)}"
        )
        self.query_one("#sidebar-content", Static).update(content)

    def begin_round(self, round_number: int, max_rounds: int) -> None:
        """后台线程调用；为新一轮创建可折叠块。"""
        try:
            self.call_from_thread(self._begin_round_impl, round_number, max_rounds)
        except Exception:
            pass

    def _begin_round_impl(self, round_number: int, max_rounds: int) -> None:
        container = self.query_one("#log", VerticalScroll)
        for widget in self._round_widgets:
            if not widget.collapsed:
                widget.collapsed = True
        content = RichLog(highlight=True, markup=True, wrap=True)
        collapsible = Collapsible(
            content, title=f"第 {round_number}/{max_rounds} 轮", collapsed=True
        )
        container.mount(collapsible)
        container.scroll_end(animate=False)
        self._round_widgets.append(collapsible)
        self._current_round_log = content
        self._current_round_summary = [f"第 {round_number}/{max_rounds} 轮"]

    def round_line(self, text: str) -> None:
        """后台线程调用；写入当前轮内容。"""
        try:
            self.call_from_thread(self._round_line_impl, text)
        except Exception:
            pass

    def _round_line_impl(self, text: str) -> None:
        if self._current_round_log is not None:
            self._current_round_log.write(text)
        else:
            # 尚无轮次（规划/审计准备阶段）时回退到总日志，避免错误被吞。
            self._log_line_impl(text)

    def round_summary(self, summary: str) -> None:
        """后台线程调用；更新当前轮标题摘要。"""
        try:
            self.call_from_thread(self._round_summary_impl, summary)
        except Exception:
            pass

    def _round_summary_impl(self, summary: str) -> None:
        self._current_round_summary.append(summary)
        if not self._round_widgets:
            return
        title = " · ".join(self._current_round_summary[-3:])
        self._round_widgets[-1].title = title

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
            self.log_line_safe(
                f"[red]任务异常：{type(exc).__name__}: {exc}[/red]"
            )
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
        self.refresh_sidebar()

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
            elif command.name == "model":
                self._choose_model()
            elif command.name == "session":
                self._handle_session(command.subcommand, command.argument)
            elif command.name == "permission":
                self._permission(command.argument)
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
        self.log_line(f"[cyan]权限[/cyan] {self.runtime.permission_level}")

    def _clear_current(self) -> None:
        # 确认必须在线程 worker 中执行：UI 线程内 _confirm 会阻塞事件循环并死锁。
        self.run_worker(
            self._clear_confirm_worker, thread=True, exclusive=True, name="session-clear"
        )

    def _clear_confirm_worker(self) -> None:
        if not self._confirm("清除当前会话的上下文和摘要？"):
            self.log_line_safe("[dim]已取消清除[/dim]")
            return
        if self.runtime is not None:
            self.runtime.clear_current()
            self.log_line_safe("[yellow]当前会话记忆已清除[/yellow]")

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
        # 确认必须在线程 worker 中执行：UI 线程内 _confirm 会阻塞事件循环并死锁。
        self.run_worker(self._undo_worker, thread=True, exclusive=True, name="session-undo")

    def _undo_worker(self) -> None:
        if not self._confirm("撤销最近一条任务的全部文件修改？"):
            self.log_line_safe("[dim]已取消撤销[/dim]")
            return
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

    def _permission(self, argument: str | None) -> None:
        if argument is None:
            self._choose_permission()
            return
        try:
            level = self.runtime.set_permission(argument)
        except SessionRuntimeError as exc:
            self.log_line(f"[red]权限操作失败：{exc}[/red]")
            return
        self.log_line(f"[yellow]权限级别已切换：{level}[/yellow]")
        self._refresh_sidebar_impl()

    def _choose_permission(self) -> None:
        options = ("strict", "relaxed", "fullaccess")
        current = self.runtime.permission_level

        def respond(value: str | None) -> None:
            if value is None or value == current:
                return
            try:
                level = self.runtime.set_permission(value)
            except SessionRuntimeError as exc:
                self.log_line(f"[red]权限操作失败：{exc}[/red]")
                return
            self.log_line(f"[yellow]权限级别已切换：{level}[/yellow]")
            self._refresh_sidebar_impl()

        self.push_screen(
            OptionListScreen("选择权限级别", options, current), respond
        )

    def _choose_model(self) -> None:
        providers = ("openai", "deepseek", "glm")
        current = self.runtime.current.record.provider

        def respond(value: str | None) -> None:
            if value is None or value == current:
                return
            self.run_worker(
                lambda: self._change_model_worker(value),
                thread=True, exclusive=True, name="model-switch",
            )

        self.push_screen(
            OptionListScreen("选择 Provider", providers, current), respond
        )

    def _change_model_worker(self, provider: str) -> None:
        if self.runtime is None:
            return
        try:
            self.runtime.change_model(provider)
        except SessionRuntimeError as exc:
            self.log_line_safe(f"[red]模型切换失败：{exc}[/red]")
            return
        self.log_line_safe(f"[yellow]已切换到 Provider：{provider}[/yellow]")
        self.refresh_sidebar()

    def _handle_session(self, subcommand: str | None, argument: str | None) -> None:
        if self.runtime is None:
            return
        if subcommand is None:
            self._choose_session()
        elif subcommand == "current":
            self._show_status()
        elif subcommand == "new":
            self.run_worker(
                lambda: self._session_new_worker(argument or "default"),
                thread=True, exclusive=True, name="session-new",
            )
        elif subcommand == "rename":
            if not argument:
                self.log_line("[dim]用法：/session rename <名称>[/dim]")
                return
            try:
                self.runtime.rename_current(argument)
            except SessionRuntimeError as exc:
                self.log_line(f"[red]重命名失败：{exc}[/red]")
                return
            self.log_line(f"[yellow]当前会话已重命名为：{argument}[/yellow]")

    def _session_new_worker(self, name: str) -> None:
        if self.runtime is None:
            return
        self.runtime.create(name)
        self.log_line_safe(f"[yellow]已创建并切换到新会话：{name}[/yellow]")
        self.refresh_sidebar()

    def _choose_session(self) -> None:
        sessions = self.runtime.store.list_all()
        if not sessions:
            self.log_line("[dim]没有可切换的会话。[/dim]")
            return
        current_id = self.runtime.current.record.id
        options = [
            f"{record.name}  ({record.provider} · {record.model})"
            for record in sessions
        ]
        current_name = next(
            (record.name for record in sessions if record.id == current_id), None
        )

        def respond(value: str | None) -> None:
            if value is None:
                return
            index = options.index(value)
            record = sessions[index]
            if record.id == current_id:
                return
            self.run_worker(
                lambda: self._switch_worker(record),
                thread=True, exclusive=True, name="session-switch",
            )

        self.push_screen(
            OptionListScreen("选择会话", options, current_name), respond
        )

    def _switch_worker(self, record: SessionRecord) -> None:
        try:
            self.runtime.switch(
                record.id,
                confirm=lambda workspace: self._confirm(
                    f"目标工作区为 {workspace}。确认切换？"
                ),
            )
        except SessionRuntimeError as exc:
            self.log_line_safe(f"[red]会话切换失败：{exc}[/red]")
            return
        self.log_line_safe(f"[yellow]已切换到会话：{record.name}[/yellow]")
        self.refresh_sidebar()

    # ---- 退出 ----

    def action_cancel(self) -> None:
        self.query_one(Input).value = ""

    def action_quit(self) -> None:
        code = 1
        if self.runtime is not None:
            code = 0 if self.runtime.retry_persist() else 1
        self.exit(code)
