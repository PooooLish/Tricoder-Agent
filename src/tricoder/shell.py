"""交互式 Shell：本地处理斜杠命令，仅将普通文本交给 SessionRuntime。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from tricoder.commands import CommandError, ParsedCommand, is_slash_command, parse_command
from tricoder.changes import UndoExecution, UndoPreview
from tricoder.models import RunResult
from tricoder.session_runtime import SessionRuntimeError


class ShellUI(Protocol):
    """Shell 依赖的最小 UI 协议，便于通过注入测试而不依赖真实终端。"""

    def show_shell_start(self, record: object) -> None:
        """显示当前会话的交互启动信息。"""

    def show_help(self) -> None:
        """显示本地命令帮助。"""

    def show_status(self, status: object, active: object) -> None:
        """显示当前 Session 状态。"""

    def choose_session(self, sessions: list[object], current_id: str) -> str | None:
        """返回选择的 Session ID，空输入或无效编号返回 None。"""

    def choose_model(self, previews: Mapping[str, str], provider: str) -> str | None:
        """返回选择的 Provider，空输入或无效编号返回 None。"""

    def confirm(self, prompt: str) -> bool:
        """仅在用户明确输入 y 或 yes 时返回 True。"""

    def show_memory_warning(self, warning: str) -> None:
        """显示记忆未持久化警告。"""

    def show_notice(self, message: str) -> None:
        """显示无需中断循环的一般提示。"""

    def show_diff(self, diff: str, *, title: str) -> None:
        """以字面形式显示任务变更或撤销预览。"""

    def show_error(self, title: str, message: str) -> None:
        """显示可恢复的本地错误。"""

    def show_run_result(self, result: RunResult) -> None:
        """显示普通交互任务的结构化结果。"""


class RuntimeLike(Protocol):
    """Shell 所需的运行时协议，避免将测试与具体装配耦合。"""

    current: object
    store: object

    def run_task(self, task: str) -> RunResult:
        """运行普通用户任务。"""

    def diff_latest(self) -> str | None:
        """返回最近任务的正向差异；没有历史时返回 None。"""

    def prepare_undo(self) -> UndoPreview:
        """校验并生成最近任务的反向差异预览。"""

    def undo_latest(self) -> UndoExecution:
        """在确认后再次校验并撤销最近任务。"""

    def switch(self, session_id: str, *, confirm: Callable[[object], bool]) -> object:
        """切换 Session。"""

    def create(self, name: str) -> object:
        ...

    def rename_current(self, name: str) -> object:
        ...

    def clear_current(self) -> None:
        ...

    def change_model(self, provider: str) -> object:
        ...

    def preview_models(self) -> Mapping[str, str]:
        ...

    def retry_persist(self) -> bool:
        ...

    def status(self) -> object:
        ...


class InteractiveShell:
    """维护输入循环并在本地分发命令的轻量交互层。"""

    def __init__(
        self,
        runtime: RuntimeLike,
        ui: ShellUI,
        *,
        input_fn: Callable[[str], str] = input,
        prompt: str = "tricoder> ",
    ) -> None:
        self.runtime = runtime
        self.ui = ui
        self.input_fn = input_fn
        self.prompt = prompt

    def run(self) -> int:
        """运行至用户退出；输入阶段的 Ctrl+C 和 EOF 均有稳定语义。"""
        self.ui.show_shell_start(self.runtime.current.record)  # type: ignore[attr-defined]
        while True:
            try:
                text = self.input_fn(self.prompt).strip()
            except KeyboardInterrupt:
                self.ui.show_notice("已清空当前输入")
                continue
            except EOFError:
                return self._exit()
            if not text:
                continue
            try:
                code = self.execute(text)
            except KeyboardInterrupt:
                self.ui.show_notice("已取消当前操作")
                continue
            except EOFError:
                return self._exit()
            if code is not None:
                return code

    def execute(self, text: str) -> int | None:
        """执行一次输入，供循环和单元测试复用。"""
        if not is_slash_command(text):
            try:
                result = self.runtime.run_task(text)
                self.ui.show_run_result(result)
            except SessionRuntimeError as exc:
                self.ui.show_error("任务运行失败", str(exc))
            return None

        try:
            command = parse_command(text)
        except CommandError as exc:
            self.ui.show_error("命令错误", f"{exc} 请使用 /help 查看可用命令。")
            return None

        try:
            return self._execute_command(command)
        except SessionRuntimeError as exc:
            self.ui.show_error("会话操作失败", str(exc))
            return None
        except (OSError, ValueError) as exc:
            self.ui.show_error("会话操作失败", str(exc))
            return None

    def _execute_command(self, command: ParsedCommand) -> int | None:
        if command.name == "help":
            self.ui.show_help()
        elif command.name == "status":
            self._show_status()
        elif command.name == "model":
            self._choose_model()
        elif command.name == "clear":
            self._clear_current()
        elif command.name == "diff":
            self._show_diff()
        elif command.name == "undo":
            self._undo_latest()
        elif command.name == "session":
            self._handle_session(command)
        elif command.name == "exit":
            return self._exit()
        return None

    def _handle_session(self, command: ParsedCommand) -> None:
        if command.subcommand is None:
            self._choose_session()
        elif command.subcommand == "new":
            self.runtime.create(command.argument or "")
            self.ui.show_notice("已创建并切换到新会话")
        elif command.subcommand == "current":
            self._show_status()
        elif command.subcommand == "rename":
            self.runtime.rename_current(command.argument or "")
            self.ui.show_notice("当前会话已重命名")

    def _show_status(self) -> None:
        self.ui.show_status(self.runtime.status(), self.runtime.current)

    def _choose_session(self) -> None:
        sessions = self.runtime.store.list_all()  # type: ignore[attr-defined]
        current_id = self.runtime.current.record.id  # type: ignore[attr-defined]
        selected_id = self.ui.choose_session(sessions, current_id)
        if selected_id is None:
            return
        if selected_id not in {record.id for record in sessions}:
            self.ui.show_error("会话错误", "选择的会话编号无效。")
            return

        self.runtime.switch(
            selected_id,
            confirm=lambda workspace: self.ui.confirm(
                f"目标工作区为 {workspace}。确认切换？[y/N] "
            ),
        )

    def _choose_model(self) -> None:
        record = self.runtime.current.record  # type: ignore[attr-defined]
        provider = self.ui.choose_model(self.runtime.preview_models(), record.provider)
        if provider is None:
            return
        self.runtime.change_model(provider)
        self.ui.show_notice("模型已切换")

    def _clear_current(self) -> None:
        if not self.ui.confirm("清除当前会话的上下文和摘要？[y/N] "):
            self.ui.show_notice("已取消清除")
            return
        self.runtime.clear_current()
        self.ui.show_notice("当前会话记忆已清除")

    def _show_diff(self) -> None:
        """只读取最近一次任务变更，绝不进入 Agent 任务通道。"""
        diff = self.runtime.diff_latest()
        if diff is None:
            self.ui.show_notice("当前 Session 没有最近任务变更。")
            return
        self.ui.show_diff(diff, title="最近任务变更")

    def _undo_latest(self) -> None:
        """先展示完整反向差异，收到明确确认后才请求 Runtime 撤销。"""
        preview = self.runtime.prepare_undo()
        self.ui.show_diff(preview.diff, title="撤销预览")
        if not self.ui.confirm("撤销最近一条任务的全部文件修改？[y/N] "):
            self.ui.show_notice("已取消撤销。")
            return

        execution = self.runtime.undo_latest()
        if execution.ok:
            self.ui.show_notice("已撤销最近一条任务的全部文件修改。")
            return
        if execution.conflicts:
            paths = "、".join(execution.conflicts)
            self.ui.show_error("撤销冲突", f"检测到文件冲突，未执行撤销：{paths}")
            return
        if execution.compensation_failed:
            paths = "、".join(execution.compensation_failed)
            self.ui.show_error("撤销失败", f"撤销未完成且补偿失败：{paths}")
            return
        self.ui.show_error("撤销失败", "撤销未完成，文件未被修改。")

    def _exit(self) -> int:
        """退出前重试持久化；失败时明确告警并返回非零状态。"""
        if self.runtime.retry_persist():
            return 0
        status = self.runtime.status()
        warning = getattr(status, "warning", "") or "本次记忆未持久化"
        self.ui.show_memory_warning(warning)
        return 1
