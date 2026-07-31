"""交互 Shell 的本地命令和可恢复输入行为测试。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest

from tricoder.models import RunResult, SessionMemory, SessionRecord
from tricoder.session_runtime import RuntimeStatus, SessionRuntimeError
from tricoder.shell import InteractiveShell


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
        self.first = make_record("first", "first", "D:/workspace/first")
        self.second = make_record("second", "second", "D:/workspace/second", "glm", "glm-test")
        self.sessions = [self.first, self.second]
        self.tasks: list[str] = []
        self.persist_ok = True
        self.fail_model = False
        self.clear_calls = 0
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
        self.tasks.append(task)
        return self.run_result

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

    def __init__(self) -> None:
        self.text: list[str] = []
        self.session_choice: str | None = None
        self.model_choice: str | None = None
        self.answers: list[str] = []
        self.run_results: list[RunResult] = []

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
        return bool(self.answers) and self.answers.pop(0).strip().lower() in {"y", "yes"}

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
        self.ui = FakeUI()

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
