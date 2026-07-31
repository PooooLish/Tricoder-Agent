"""基于 Rich 的终端界面与审批交互。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from tricoder.models import AppConfig, RunResult, SessionRecord, ToolAction, ToolResult

_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "glm": "GLM",
}


class TerminalUI:
    """集中管理 CLI 的视觉层级、状态与审批提示。"""

    def __init__(
        self,
        *,
        console: Console,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        self.console = console
        self.input_fn = input_fn
        self._status: Status | None = None

    def show_start(self, task: str, config: AppConfig) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        table.add_row("任务", Text(task))
        table.add_row(
            "Provider",
            Text(
                f"{_PROVIDER_LABELS.get(config.provider.name, config.provider.name)}"
                f" · {config.provider.model}"
            ),
        )
        table.add_row("工作区", Text(str(config.workspace)))
        table.add_row("密钥来源", Text(config.key_source))
        table.add_row("模式", "只读" if config.read_only else "可编辑 · 人工审批")
        self.console.print(
            Panel(
                table,
                title="[bold cyan]TriCoder CLI[/bold cyan]",
                border_style="cyan",
                box=box.ROUNDED,
            )
        )

    def show_doctor(self, config: AppConfig, key_name: str) -> None:
        table = Table(
            title="配置检查",
            box=box.ROUNDED,
            header_style="bold cyan",
            show_lines=False,
        )
        table.add_column("项目", style="dim")
        table.add_column("值")
        table.add_column("状态", justify="right")
        table.add_row("Provider", Text(config.provider.name), "[green]可用[/green]")
        table.add_row("Model", Text(config.provider.model), "[green]已配置[/green]")
        table.add_row("Base URL", Text(config.provider.base_url), "[green]HTTPS[/green]")
        table.add_row(Text(key_name), "••••••••", "[green]已设置[/green]")
        table.add_row("密钥来源", Text(config.key_source), "[green]已确认[/green]")
        table.add_row("网络请求", "doctor 不访问网络", "[dim]未发送[/dim]")
        self.console.print(table)

    def show_shell_start(self, record: SessionRecord) -> None:
        """显示当前本地 Session，不暴露密钥或原始上下文。"""
        details = Table.grid(padding=(0, 2))
        details.add_column(style="dim", no_wrap=True)
        details.add_column()
        details.add_row("会话", Text(record.name))
        details.add_row("工作区", Text(str(record.workspace)))
        details.add_row("模型", Text(f"{_PROVIDER_LABELS.get(record.provider, record.provider)} · {record.model}"))
        self.console.print(Panel(details, title="[bold cyan]TriCoder 交互会话[/bold cyan]", border_style="cyan", box=box.ROUNDED))

    def show_help(self) -> None:
        """输出首版全部本地命令及其参数。"""
        table = Table(title="本地命令", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("命令", style="bold")
        table.add_column("说明")
        for command, description in (
            ("/help", "显示本帮助"),
            ("/status", "显示当前会话状态"),
            ("/model", "选择 OpenAI、DeepSeek 或 GLM"),
            ("/clear", "确认后清除当前会话上下文和摘要"),
            ("/session", "列出会话并按编号选择"),
            ("/session new <名称>", "创建并切换到新会话"),
            ("/session current", "显示当前会话详情"),
            ("/session rename <名称>", "重命名当前会话"),
            ("/exit", "保存安全记忆后退出"),
        ):
            table.add_row(command, description)
        self.console.print(table)

    def show_status(self, status: object, active: object) -> None:
        """显示状态快照，不显示任务原文、上下文内容或凭据。"""
        record = status.record  # type: ignore[attr-defined]
        config = active.config  # type: ignore[attr-defined]
        context = active.context  # type: ignore[attr-defined]
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        table.add_row("会话", Text(record.name))
        table.add_row("工作区", Text(str(record.workspace)))
        table.add_row("Provider", Text(_PROVIDER_LABELS.get(record.provider, record.provider)))
        table.add_row("模型", Text(record.model))
        table.add_row("模式", "只读" if config.read_only else "可编辑 · 人工审批")
        memory = getattr(active, "memory", None)
        table.add_row("验证", Text(getattr(memory, "verification", "未运行")))
        table.add_row("上下文消息", str(len(getattr(context, "messages", ()))))
        if status.unsaved_memory:  # type: ignore[attr-defined]
            table.add_row("记忆", Text(status.warning or "本次记忆未持久化", style="yellow"))  # type: ignore[attr-defined]
        self.console.print(Panel(table, title="[bold cyan]当前状态[/bold cyan]", border_style="cyan", box=box.ROUNDED))

    def choose_session(self, sessions: Sequence[SessionRecord], current_id: str) -> str | None:
        """列出全部 Session；仅有效的 1 基序号会返回稳定 Session ID。"""
        table = Table(title="选择会话", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("#", justify="right")
        table.add_column("会话")
        table.add_column("工作区")
        table.add_column("模型")
        table.add_column("更新时间")
        for index, record in enumerate(sessions, start=1):
            marker = " (当前)" if record.id == current_id else ""
            table.add_row(
                str(index),
                Text(f"{record.name}{marker}"),
                Text(str(record.workspace)),
                Text(f"{record.provider} · {record.model}"),
                Text(record.updated_at),
            )
        self.console.print(table)
        answer = self.input_fn("选择会话编号（留空取消）：").strip()
        if not answer:
            return None
        try:
            index = int(answer)
        except ValueError:
            self.show_notice("无效会话编号，已取消。")
            return None
        if not 1 <= index <= len(sessions):
            self.show_notice("无效会话编号，已取消。")
            return None
        return sessions[index - 1].id

    def choose_model(self, previews: Mapping[str, str], provider: str) -> str | None:
        """显示已解析模型名；切换仍由运行时完整验证 Key 并原子执行。"""
        providers = ("openai", "deepseek", "glm")
        table = Table(title="选择模型 Provider", box=box.ROUNDED, header_style="bold cyan")
        table.add_column("#", justify="right")
        table.add_column("Provider")
        table.add_column("模型")
        for index, candidate in enumerate(providers, start=1):
            current = " (当前)" if candidate == provider else ""
            table.add_row(
                str(index),
                f"{_PROVIDER_LABELS[candidate]}{current}",
                Text(previews[candidate]),
            )
        self.console.print(table)
        answer = self.input_fn("选择 Provider 编号（留空取消）：").strip()
        if not answer:
            return None
        try:
            index = int(answer)
        except ValueError:
            self.show_notice("无效 Provider 编号，已取消。")
            return None
        if not 1 <= index <= len(providers):
            self.show_notice("无效 Provider 编号，已取消。")
            return None
        return providers[index - 1]

    def confirm(self, prompt: str) -> bool:
        """所有破坏性或跨工作区操作均要求精确 y/yes 确认。"""
        return self.input_fn(prompt).strip().lower() in {"y", "yes"}

    def show_memory_warning(self, warning: str) -> None:
        """明确告知用户记忆仍仅存在于当前进程。"""
        self.console.print(Panel(Text(warning), title="[bold yellow]记忆未持久化[/bold yellow]", border_style="yellow", box=box.ROUNDED))

    def show_notice(self, message: str) -> None:
        """以字面文本显示可恢复提示，避免 Rich 解析用户内容。"""
        self._stop_status()
        self.console.print(Text(message, style="yellow"))

    def show_error(self, title: str, message: str) -> None:
        self._stop_status()
        self.console.print(
            Panel(
                Text(message),
                title=f"[bold red]{title}[/bold red]",
                border_style="red",
                box=box.ROUNDED,
            )
        )

    def show_run_result(self, result: RunResult) -> None:
        """显示交互任务结果，所有动态文本都按字面值交给 Rich。"""
        self._stop_status()
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        table.add_row("状态", "成功" if result.ok else "未完成")
        table.add_row("摘要", Text(result.summary))
        table.add_row("修改文件", str(len(result.modified_files)))
        table.add_row("验证结果", Text(result.verification))
        color = "green" if result.ok else "red"
        title = "✓ 任务完成" if result.ok else "✗ 任务未完成"
        self.console.print(
            Panel(
                table,
                title=f"[bold {color}]{title}[/bold {color}]",
                border_style=color,
                box=box.ROUNDED,
            )
        )

    def show_complete(self, result: RunResult, audit_path: Path) -> None:
        self._stop_status()
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        table.add_row("状态", "成功" if result.ok else "未完成")
        table.add_row("摘要", Text(result.summary))
        table.add_row("工具调用", str(result.tool_calls))
        table.add_row("修改文件", str(len(result.modified_files)))
        table.add_row("验证结果", result.verification)
        table.add_row("模型轮数", str(result.rounds))
        table.add_row("审计轨迹", Text(str(audit_path)))
        color = "green" if result.ok else "red"
        title = "✓ 任务完成" if result.ok else "✗ 任务未完成"
        self.console.print(
            Panel(
                table,
                title=f"[bold {color}]{title}[/bold {color}]",
                border_style=color,
                box=box.ROUNDED,
            )
        )

    def approve(self, action: str, detail: str) -> bool:
        self._stop_status()
        if action in {"edit_file", "create_file"}:
            renderable = Syntax(
                detail,
                "diff",
                theme="ansi_dark",
                word_wrap=True,
                background_color="default",
            )
            title = (
                "文件修改 · 需要审批"
                if action == "edit_file"
                else "文件创建 · 需要审批"
            )
        else:
            renderable = Text(detail)
            title = "命令执行 · 需要审批"
        self.console.print(
            Panel(
                renderable,
                title=f"[bold yellow]{title}[/bold yellow]",
                border_style="yellow",
                box=box.ROUNDED,
            )
        )
        answer = self.input_fn("允许执行？[y/N] ").strip().lower()
        allowed = answer in {"y", "yes"}
        if not allowed:
            self.console.print("[yellow]已拒绝该操作。[/yellow]")
        return allowed

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        self._stop_status()
        message = f"第 {round_number}/{max_rounds} 轮：正在请求模型……"
        if self.console.is_terminal:
            self._status = self.console.status(message, spinner="dots", spinner_style="cyan")
            self._status.start()
        else:
            self.console.print(Text(message, style="cyan"))

    def on_action(self, action: ToolAction) -> None:
        self._stop_status()
        line = Text()
        line.append("● ", style="cyan")
        line.append(action.tool, style="bold")
        line.append(f"\n  目的：{action.reason}", style="dim")
        self.console.print(line)

    def on_tool_result(
        self,
        action: ToolAction,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        icon = "✓" if result.ok else "✗"
        style = "green" if result.ok else "red"
        state = "完成" if result.ok else "失败"
        self.console.print(
            Text(
                f"  {icon} {state} · {len(result.output):,} 字符 · {duration_ms} ms",
                style=style,
            )
        )

    def on_error(self, message: str) -> None:
        self._stop_status()
        self.console.print(Text(f"  ✗ {message}", style="red"))

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
