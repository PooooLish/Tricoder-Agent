"""交互 Shell 的本地命令和可恢复输入行为测试。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest

from tricoder.models import RunResult, SessionMemory, SessionRecord
from tricoder.changes import UndoExecution, UndoPreview
from tricoder.session_runtime import RuntimeStatus, SessionRuntimeError
from tricoder.shell import InteractiveShell


EXPECTED_DIFF = "--- src/app.py\n+++ src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
EXPECTED_REVERSE_DIFF = "--- src/app.py\n+++ src/app.py\n@@ -1 +1 @@\n-new\n+old\n"


class SpyProvider:
    """记录正常任务路径是否意外触及 Provider。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def complete(self) -> None:
        self.calls += 1
        self.events.append("provider")


class SpyAgent:
    """记录正常任务路径是否意外触及 Agent。"""

    def __init__(self, provider: SpyProvider, events: list[str]) -> None:
        self.provider = provider
        self.events = events
        self.calls = 0

    def run(self) -> None:
        self.calls += 1
        self.events.append("agent")
        self.provider.complete()


def make_record(
    session_id: str,
    name: str,
    workspace: str,
    provider: str = "openai",
    model: str = "gpt-test",
) -> SessionRecord:
    """构造不依赖 SQLite 的稳定 Session 记录。"""
    return SessionRecord(
        session_id,
        name,
        Path(workspace).resolve(),
        provider,
        model,
        "2026-07-31T00:00:00+00:00",
        "2026-07-31T00:00:00+00:00",
    )


class FakeRuntime:
    """记录 Shell 调用，确保测试不触发 Provider、文件或 SQLite。"""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.provider = SpyProvider(self.events)
        self.agent = SpyAgent(self.provider, self.events)
        self.first = make_record("first", "first", "D:/workspace/first")
        self.second = make_record("second", "second", "D:/workspace/second", "glm", "glm-test")
        self.sessions = [self.first, self.second]
        self.tasks: list[str] = []
        self.run_task_calls = 0
        self.persist_ok = True
        self.fail_model = False
        self.clear_calls = 0
        self.diff_latest_calls = 0
        self.prepare_undo_calls = 0
        self.undo_latest_calls = 0
        self.diff_result: str | None = EXPECTED_DIFF
        self.undo_preview = UndoPreview(EXPECTED_REVERSE_DIFF, ("src/app.py",))
        self.undo_execution = UndoExecution(True, ("src/app.py",))
        self.prepare_undo_error: SessionRuntimeError | None = None
        self.undo_latest_error: SessionRuntimeError | None = None
        self.run_result = RunResult(
            True,
            "任务完成",
            2,
            modified_files=("src/app.py",),
            verification="passed",
        )
        self.model_previews = {
            "openai": "gpt-preview",
            "deepseek": "deepseek-preview",
            "glm": "glm-preview",
        }
        self.current = self._active(self.first)
        self.store = SimpleNamespace(list_all=lambda: list(self.sessions))

    @staticmethod
    def _active(record: SessionRecord) -> SimpleNamespace:
        return SimpleNamespace(
            record=record,
            memory=SessionMemory(),
            context=SimpleNamespace(messages=()),
            config=SimpleNamespace(read_only=False),
        )

    def run_task(self, task: str) -> RunResult:
        self.run_task_calls += 1
        self.tasks.append(task)
        self.events.append("run_task")
        self.agent.run()
        return self.run_result

    def diff_latest(self) -> str | None:
        self.diff_latest_calls += 1
        self.events.append("diff_latest")
        return self.diff_result

    def prepare_undo(self) -> UndoPreview:
        self.prepare_undo_calls += 1
        self.events.append("prepare_undo")
        if self.prepare_undo_error is not None:
            raise self.prepare_undo_error
        return self.undo_preview

    def undo_latest(self) -> UndoExecution:
        self.undo_latest_calls += 1
        self.events.append("undo_latest")
        if self.undo_latest_error is not None:
            raise self.undo_latest_error
        return self.undo_execution

    def switch(self, session_id: str, *, confirm):  # type: ignore[no-untyped-def]
        target = next(item for item in self.sessions if item.id == session_id)
        if target.workspace != self.current.record.workspace and not confirm(target.workspace):
            return self.current
        self.current = self._active(target)
        return self.current

    def create(self, name: str) -> SimpleNamespace:
        record = make_record("created", name, str(self.current.record.workspace))
        self.sessions.insert(0, record)
        self.current = self._active(record)
        return self.current

    def rename_current(self, name: str) -> SessionRecord:
        original = self.current.record
        renamed = make_record(
            original.id, name, str(original.workspace), original.provider, original.model
        )
        self.sessions = [renamed if item.id == original.id else item for item in self.sessions]
        self.current = self._active(renamed)
        return renamed

    def clear_current(self) -> None:
        self.clear_calls += 1

    def change_model(self, provider: str) -> SimpleNamespace:
        if self.fail_model:
            raise SessionRuntimeError("缺少模型配置")
        original = self.current.record
        changed = make_record(original.id, original.name, str(original.workspace), provider, f"{provider}-test")
        self.sessions = [changed if item.id == original.id else item for item in self.sessions]
        self.current = self._active(changed)
        return self.current

    def preview_models(self) -> dict[str, str]:
        return dict(self.model_previews)

    def retry_persist(self) -> bool:
        return self.persist_ok

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            self.current.record,
            not self.persist_ok,
            "本次记忆未持久化" if not self.persist_ok else "",
        )


class FakeUI:
    """可观测的 UI 替身，输入由测试直接安排。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.text: list[str] = []
        self.session_choice: str | None = None
        self.model_choice: str | None = None
        self.answers: list[str] = []
        self.confirm_calls = 0
        self.run_results: list[RunResult] = []
        self.diffs: list[tuple[str, str]] = []

    def show_shell_start(self, _record: SessionRecord) -> None:
        self.text.append("start")

    def show_help(self) -> None:
        self.text.append("help")

    def show_status(self, _status: RuntimeStatus, _active: object) -> None:
        self.text.append("status")

    def choose_session(self, _sessions, _current_id: str) -> str | None:  # type: ignore[no-untyped-def]
        return self.session_choice

    def choose_model(self, previews: dict[str, str], _provider: str) -> str | None:
        self.text.extend(previews.values())
        return self.model_choice

    def confirm(self, _prompt: str) -> bool:
        self.confirm_calls += 1
        self.events.append("confirm")
        return bool(self.answers) and self.answers.pop(0).strip().lower() in {"y", "yes"}

    def show_diff(self, diff: str, *, title: str) -> None:
        self.events.append("show_diff")
        self.diffs.append((title, diff))

    def show_memory_warning(self, warning: str) -> None:
        self.text.append(warning)

    def show_notice(self, message: str) -> None:
        self.text.append(message)

    def show_error(self, title: str, message: str) -> None:
        self.text.append(f"{title}: {message}")

    def show_run_result(self, result: RunResult) -> None:
        self.run_results.append(result)


class InteractiveShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.ui = FakeUI(self.runtime.events)

    def shell(self, input_fn=lambda _prompt: "/exit") -> InteractiveShell:
        return InteractiveShell(self.runtime, self.ui, input_fn=input_fn)

    def test_slash_commands_are_local(self) -> None:
        """所有斜杠命令都不得进入 Agent 任务通道。"""
        inputs = iter(["/status", "/help", "/exit"])

        code = self.shell(lambda _prompt: next(inputs)).run()

        self.assertEqual(0, code)
        self.assertEqual([], self.runtime.tasks)
        self.assertIn("status", self.ui.text)
        self.assertIn("help", self.ui.text)

    def test_regular_text_is_sent_to_runtime(self) -> None:
        """只有普通输入才可调用当前 Session 的 Agent。"""
        inputs = iter(["检查模块", "/exit"])

        self.assertEqual(0, self.shell(lambda _prompt: next(inputs)).run())

        self.assertEqual(["检查模块"], self.runtime.tasks)
        self.assertEqual([self.runtime.run_result], self.ui.run_results)

    def test_failed_run_result_is_still_displayed(self) -> None:
        """防止 Agent 返回未完成结果时 Shell 静默丢弃摘要与验证状态。"""
        self.runtime.run_result = RunResult(
            False,
            "任务未完成",
            1,
            verification="failed",
        )

        self.shell().execute("检查失败场景")

        self.assertEqual([self.runtime.run_result], self.ui.run_results)

    def test_diff_is_local_and_shows_latest_task_change(self) -> None:
        """/diff 必须只查询运行时账本，不得调用 Agent 或 Provider。"""
        self.shell().execute("/diff")

        self.assertEqual(1, self.runtime.diff_latest_calls)
        self.assertEqual(0, self.runtime.run_task_calls)
        self.assertEqual(0, self.runtime.agent.calls)
        self.assertEqual(0, self.runtime.provider.calls)
        self.assertEqual(["diff_latest", "show_diff"], self.runtime.events)
        self.assertEqual([("最近任务变更", EXPECTED_DIFF)], self.ui.diffs)

    def test_diff_without_history_shows_stable_notice(self) -> None:
        """没有已封存变更时 /diff 要留在本地并给出可读提示。"""
        self.runtime.diff_result = None

        self.shell().execute("/diff")

        self.assertEqual(0, self.runtime.run_task_calls)
        self.assertEqual([], self.ui.diffs)
        self.assertIn("当前 Session 没有最近任务变更。", self.ui.text)

    def test_undo_previews_then_confirms_and_executes_locally(self) -> None:
        """/undo 必须先完整展示反向 diff，再仅在 y/yes 时执行。"""
        self.ui.answers = ["yes"]

        self.shell().execute("/undo")

        self.assertEqual(1, self.runtime.prepare_undo_calls)
        self.assertEqual(
            ["prepare_undo", "show_diff", "confirm", "undo_latest"],
            self.runtime.events,
        )
        self.assertEqual([("撤销预览", EXPECTED_REVERSE_DIFF)], self.ui.diffs)
        self.assertEqual(1, self.ui.confirm_calls)
        self.assertEqual(1, self.runtime.undo_latest_calls)
        self.assertEqual(0, self.runtime.run_task_calls)
        self.assertEqual(0, self.runtime.agent.calls)
        self.assertEqual(0, self.runtime.provider.calls)
        self.assertIn("已撤销最近一条任务的全部文件修改。", self.ui.text)

    def test_undo_rejection_never_executes_after_preview(self) -> None:
        """拒绝撤销后不得触发任何文件写入。"""
        self.ui.answers = ["no"]

        self.shell().execute("/undo")

        self.assertEqual([("撤销预览", EXPECTED_REVERSE_DIFF)], self.ui.diffs)
        self.assertEqual(["prepare_undo", "show_diff", "confirm"], self.runtime.events)
        self.assertEqual(1, self.ui.confirm_calls)
        self.assertEqual(0, self.runtime.undo_latest_calls)
        self.assertEqual(0, self.runtime.run_task_calls)
        self.assertEqual(0, self.runtime.agent.calls)
        self.assertEqual(0, self.runtime.provider.calls)
        self.assertIn("已取消撤销。", self.ui.text)

    def test_undo_reports_prepare_errors_without_confirmation(self) -> None:
        """无历史和只读错误均由 Runtime 给出，Shell 不应继续确认或执行。"""
        for message in ("当前 Session 没有可撤销的最近任务", "只读模式禁止撤销"):
            with self.subTest(message=message):
                self.runtime.prepare_undo_error = SessionRuntimeError(message)

                self.shell().execute("/undo")

                self.assertEqual(0, self.ui.confirm_calls)
                self.assertEqual(0, self.runtime.undo_latest_calls)
                self.assertTrue(any(message in item for item in self.ui.text))
                self.runtime.prepare_undo_error = None
                self.ui.text.clear()

    def test_undo_reports_conflict_after_second_runtime_validation(self) -> None:
        """确认后再次校验产生冲突时，Shell 只能报告而不能伪报成功。"""
        self.ui.answers = ["y"]
        self.runtime.undo_execution = UndoExecution(False, (), ("src/app.py",))

        self.shell().execute("/undo")

        self.assertEqual(1, self.runtime.undo_latest_calls)
        self.assertEqual(0, self.runtime.run_task_calls)
        self.assertTrue(any("冲突" in item for item in self.ui.text))
        self.assertFalse(any("已撤销" in item for item in self.ui.text))

    def test_undo_reports_compensation_failure_with_literal_path(self) -> None:
        """补偿失败只显示稳定结果和路径，绝不伪报成功或泄露源码。"""
        path = "src/[bold red]not markup[/bold red].py"
        self.ui.answers = ["yes"]
        self.runtime.undo_execution = UndoExecution(False, (), (), (path,))

        self.shell().execute("/undo")

        self.assertEqual(
            ["prepare_undo", "show_diff", "confirm", "undo_latest"],
            self.runtime.events,
        )
        self.assertEqual(0, self.runtime.agent.calls)
        self.assertEqual(0, self.runtime.provider.calls)
        self.assertEqual([f"撤销失败: 撤销未完成且补偿失败：{path}"], self.ui.text)
        self.assertFalse(any("已撤销" in item for item in self.ui.text))
        self.assertFalse(any("-new" in item or "+old" in item for item in self.ui.text))

    def test_undo_reports_generic_failure_without_success_or_source(self) -> None:
        """无冲突且无补偿失败时使用稳定失败提示，不附带预览源码。"""
        self.ui.answers = ["yes"]
        self.runtime.undo_execution = UndoExecution(False, ())

        self.shell().execute("/undo")

        self.assertEqual(
            ["prepare_undo", "show_diff", "confirm", "undo_latest"],
            self.runtime.events,
        )
        self.assertEqual(0, self.runtime.agent.calls)
        self.assertEqual(0, self.runtime.provider.calls)
        self.assertEqual(["撤销失败: 撤销未完成，文件未被修改。"], self.ui.text)
        self.assertFalse(any("已撤销" in item or "-new" in item or "+old" in item for item in self.ui.text))

    def test_session_selection_confirms_cross_workspace(self) -> None:
        """跨工作区拒绝和确认分别保持与替换当前 Session。"""
        self.ui.session_choice = self.runtime.second.id
        self.ui.answers = ["n"]

        self.shell().execute("/session")
        self.assertEqual(self.runtime.first.id, self.runtime.current.record.id)

        self.ui.answers = ["yes"]
        self.shell().execute("/session")
        self.assertEqual(self.runtime.second.id, self.runtime.current.record.id)

    def test_session_commands_create_show_and_rename_current_session(self) -> None:
        """创建、查看和重命名都留在本地运行时边界。"""
        shell = self.shell()

        shell.execute("/session new refactor")
        shell.execute("/session current")
        shell.execute("/session rename stable name")

        self.assertEqual("stable name", self.runtime.current.record.name)
        self.assertIn("status", self.ui.text)
        self.assertEqual([], self.runtime.tasks)

    def test_invalid_session_selection_keeps_current_session(self) -> None:
        """无效 UI 返回值不得切换或触发 Provider。"""
        self.ui.session_choice = "not-a-session"

        self.shell().execute("/session")

        self.assertEqual(self.runtime.first.id, self.runtime.current.record.id)
        self.assertTrue(any("会话" in item for item in self.ui.text))
        self.assertEqual([], self.runtime.tasks)

    def test_clear_requires_exact_yes_or_y(self) -> None:
        """任何非 y/yes 确认都不能清空 Session 记忆。"""
        self.ui.answers = ["Yup", "yes"]
        shell = self.shell()

        shell.execute("/clear")
        self.assertEqual(0, self.runtime.clear_calls)
        shell.execute("/clear")
        self.assertEqual(1, self.runtime.clear_calls)

    def test_model_configuration_failure_keeps_current_selection(self) -> None:
        """模型配置失败时 Shell 仅报告错误，运行时 Session 不变。"""
        self.ui.model_choice = "deepseek"
        self.runtime.fail_model = True

        self.shell().execute("/model")

        self.assertEqual("openai", self.runtime.current.record.provider)
        self.assertTrue(any("模型" in item for item in self.ui.text))

    def test_model_uses_runtime_preview_for_all_providers_before_switching(self) -> None:
        """/model 先展示三项无密钥预览，再由 Runtime 完整校验并切换。"""
        self.ui.model_choice = "glm"

        self.shell().execute("/model")

        self.assertEqual("glm", self.runtime.current.record.provider)
        self.assertEqual(
            ["gpt-preview", "deepseek-preview", "glm-preview"],
            [item for item in self.ui.text if item.endswith("-preview")],
        )

    def test_eof_and_keyboard_interrupt_have_stable_exit_behavior(self) -> None:
        """输入阶段 Ctrl+C 继续循环，EOF 走与 /exit 相同的安全退出。"""
        outcomes = iter([KeyboardInterrupt(), EOFError()])

        def read(_prompt: str) -> str:
            result = next(outcomes)
            raise result

        self.assertEqual(0, self.shell(read).run())
        self.assertTrue(any("清空" in item for item in self.ui.text))

    def test_exit_retries_unsaved_memory_and_returns_nonzero_on_failure(self) -> None:
        """退出前必须再尝试持久化，失败时保留明确警告和非零状态。"""
        self.runtime.persist_ok = False

        code = self.shell().execute("/exit")

        self.assertEqual(1, code)
        self.assertTrue(any("未持久化" in item for item in self.ui.text))


if __name__ == "__main__":
    unittest.main()
