"""Textual TUI 的启动、任务流与审批模态测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Input, Static

from tricoder.agent import PLANNING_PROMPT
from tricoder.core.events import TextDelta
from tricoder.models import ProviderConfig, ProviderResponse, ToolCall
from tricoder.providers import ProviderError
from tricoder.session_runtime import RuntimeOptions, SessionRuntime
from tricoder.sessions import SessionStore
from tricoder.tui import ApprovalScreen, OptionListScreen, TricoderApp, TuiObserver


class FakeProvider:
    """按序返回预设响应，不访问网络；规划请求透明返回固定计划。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(
        self,
        messages: object,
        tools: tuple = (),
    ) -> ProviderResponse:
        if (
            not tools
            and messages
            and getattr(messages[-1], "content", None) == PLANNING_PROMPT
        ):
            return ProviderResponse(
                content='{"steps": ["步骤 1", "步骤 2", "步骤 3"]}'
            )
        self.calls += 1
        return self._responses.pop(0)


def _provider_factory(responses: list[ProviderResponse]):
    provider = FakeProvider(responses)

    def build(config: ProviderConfig, timeout: float) -> FakeProvider:
        return provider

    return build


class TricoderTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_tui_observer_forwards_streaming_text_as_literal_text(self) -> None:
        """类型化增量通过线程安全桥进入 TUI，且保留 Rich 标记字面值。"""

        class RecordingApp:
            def __init__(self) -> None:
                self.lines: list[object] = []

            def round_line(self, value: object) -> None:
                self.lines.append(value)

        app = RecordingApp()
        observer = TuiObserver(app)  # type: ignore[arg-type]

        observer(TextDelta("[conceal]secret[/conceal]"))

        self.assertEqual(1, len(app.lines))
        self.assertEqual("[conceal]secret[/conceal]", app.lines[0].plain)

    async def test_cancel_action_signals_active_runtime(self) -> None:
        """Ctrl+C 在任务活动时必须取消任务，而不只是清空输入框。"""
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(
                app.runtime, "cancel_current", return_value=True
            ) as cancel:
                app.action_cancel()
                await pilot.pause()

            cancel.assert_called_once_with()
            self.assertTrue(any("正在取消" in line for line in app._lines))

    async def test_quit_action_cancels_active_runtime_before_exit(self) -> None:
        """退出活动任务时先发取消信号，且不得并发进入持久化临界区。"""

        class Runtime:
            def __init__(self) -> None:
                self.cancelled = False
                self.persisted = False

            def cancel_current(self) -> bool:
                self.cancelled = True
                return True

            def retry_persist(self) -> bool:
                self.persisted = True
                return True

        class App:
            def __init__(self) -> None:
                self.runtime = Runtime()
                self.exit_code: int | None = None

            def exit(self, code: int) -> None:
                self.exit_code = code

        app = App()

        TricoderApp.action_quit(app)  # type: ignore[arg-type]

        self.assertTrue(app.runtime.cancelled)
        self.assertFalse(app.runtime.persisted)
        self.assertEqual(1, app.exit_code)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "def answer():\n    return 41\n", encoding="utf-8"
        )
        self.store = SessionStore(Path(self.temp.name) / "sessions.db")
        self.app: TricoderApp | None = None

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_app(self, responses: list[ProviderResponse]) -> TricoderApp:
        factory = _provider_factory(responses)

        def runtime_factory(observer, approver) -> SessionRuntime:
            return SessionRuntime(
                self.store,
                self.workspace,
                options=RuntimeOptions(environ={"OPENAI_API_KEY": "test-key"}),
                provider_factory=factory,
                approver=approver,
                observer=observer,
            )

        self.app = TricoderApp(runtime_factory)
        return self.app

    async def _submit(self, pilot, text: str) -> None:
        app = self.app
        assert app is not None
        app.query_one(Input).value = text
        await pilot.press("enter")

    async def _wait_result(self, pilot) -> ProviderResponse:
        app = self.app
        assert app is not None
        for _ in range(300):
            await pilot.pause()
            if app.last_result is not None:
                return app.last_result  # type: ignore[return-value]
        self.fail("任务未在超时内完成")

    async def _wait_approval(self, pilot) -> None:
        app = self.app
        assert app is not None
        for _ in range(300):
            await pilot.pause()
            if isinstance(app.screen, ApprovalScreen):
                return
        self.fail("审批模态未出现")

    async def test_launches_with_input_and_log(self) -> None:
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIsNotNone(app.query_one(Input))
            self.assertIsNotNone(app.query_one("#log", VerticalScroll))

    async def test_task_rounds_are_collapsible(self) -> None:
        """每轮工具调用被折叠进 Collapsible 块，避免逐行刷屏。"""
        finish = ProviderResponse(
            content=None,
            tool_calls=(ToolCall("c1", "finish", {"summary": "ok done"}),),
            finish_reason="tool_calls",
        )
        app = self._make_app([finish])
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "跑一个任务")
            result = await self._wait_result(pilot)

            self.assertTrue(result.ok)
            collapsibles = list(app.query(Collapsible))
            self.assertEqual(1, len(collapsibles))
            self.assertIn("第 1/30 轮", str(collapsibles[0].title))
            self.assertTrue(collapsibles[0].collapsed)

    async def test_run_task_returns_finish_summary(self) -> None:
        response = ProviderResponse(
            content=None,
            tool_calls=(ToolCall("c1", "finish", {"summary": "ok done"}),),
            finish_reason="tool_calls",
        )
        app = self._make_app([response])
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "跑一个任务")
            result = await self._wait_result(pilot)

        self.assertTrue(result.ok)
        self.assertIn("ok done", result.summary)

    async def test_approval_screen_rejects_write(self) -> None:
        edit = ToolCall(
            "c1",
            "edit_file",
            {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
        )
        finish = ToolCall("c2", "finish", {"summary": "done after reject"})
        app = self._make_app(
            [
                ProviderResponse(content=None, tool_calls=(edit,), finish_reason="tool_calls"),
                ProviderResponse(content=None, tool_calls=(finish,), finish_reason="tool_calls"),
            ]
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "改文件")
            await self._wait_approval(pilot)
            await pilot.press("n")
            result = await self._wait_result(pilot)

        self.assertTrue(result.ok)
        self.assertEqual(
            "def answer():\n    return 41\n",
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
        )

    async def test_sidebar_shows_session_state(self) -> None:
        """侧边状态栏显示会话、权限等运行时状态。"""
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(10):
                await pilot.pause()
            content = app.query_one("#sidebar-content", Static).content

        self.assertIn("会话", content)
        self.assertIn("权限", content)
        self.assertIn("strict", content)

    async def test_clear_confirmation_does_not_deadlock(self) -> None:
        """/clear 的确认在 worker 线程执行，UI 线程不阻塞死锁。"""
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "/clear")
            for _ in range(300):
                await pilot.pause()
                if isinstance(app.screen, ApprovalScreen):
                    break
            else:
                self.fail("/clear 确认未出现（UI 线程可能死锁）")
            await pilot.press("escape")
            await pilot.pause()

        self.assertEqual("strict", app.runtime.permission_level)

    async def test_planning_failure_error_is_visible_outside_rounds(self) -> None:
        """规划阶段错误在尚无轮次时仍显示在总日志，不被折叠块吞掉。"""

        class FailingPlanProvider:
            def complete(self, messages: object, tools: tuple = ()) -> ProviderResponse:
                if (
                    not tools
                    and getattr(messages[-1], "content", None) == PLANNING_PROMPT
                ):
                    raise ProviderError("PLAN-FAIL-MARKER")
                return ProviderResponse(
                    content=None,
                    tool_calls=(ToolCall("c1", "finish", {"summary": "done"}),),
                    finish_reason="tool_calls",
                )

        provider = FailingPlanProvider()
        factory = lambda _config, _timeout: provider  # type: ignore[misc]

        def runtime_factory(observer, approver) -> SessionRuntime:
            return SessionRuntime(
                self.store,
                self.workspace,
                options=RuntimeOptions(environ={"OPENAI_API_KEY": "test-key"}),
                provider_factory=factory,
                approver=approver,
                observer=observer,
            )

        self.app = TricoderApp(runtime_factory)
        app = self.app
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "跑一个任务")
            result = await self._wait_result(pilot)
            for _ in range(10):
                await pilot.pause()

        self.assertTrue(result.ok)
        self.assertTrue(any("规划失败" in line for line in app._lines))

    async def _wait_option_list(self, pilot) -> None:
        app = self.app
        assert app is not None
        for _ in range(300):
            await pilot.pause()
            if isinstance(app.screen, OptionListScreen):
                return
        self.fail("选择列表未出现")

    async def test_permission_command_selects_with_arrow_keys(self) -> None:
        """/permission 无参时弹方向键选择列表，选择 relaxed 后生效。"""
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "/permission")
            await self._wait_option_list(pilot)
            self.assertEqual(
                ["strict", "relaxed", "fullaccess"], app.screen._options
            )
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            for _ in range(10):
                await pilot.pause()
            self.assertEqual("relaxed", app.runtime.permission_level)
            content = app.query_one("#sidebar-content", Static).content
            self.assertIn("relaxed", content)

        self.assertEqual("relaxed", app.runtime.permission_level)

    async def test_permission_stays_strict_on_escape(self) -> None:
        """/permission 选择列表按 Esc 取消不改变级别。"""
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "/permission")
            await self._wait_option_list(pilot)
            await pilot.press("escape")
            await pilot.pause()

        self.assertEqual("strict", app.runtime.permission_level)

    async def test_task_text_renders_literally(self) -> None:
        """任务输入中的 Rich 标签必须按字面显示，不得被解析为 markup。"""
        response = ProviderResponse(
            content=None,
            tool_calls=(ToolCall("c1", "finish", {"summary": "ok"}),),
            finish_reason="tool_calls",
        )
        app = self._make_app([response])
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._submit(pilot, "修复 [conceal]secret[/conceal] 问题")
            await self._wait_result(pilot)

        app = self.app
        assert app is not None
        self.assertTrue(
            any("[conceal]secret[/conceal]" in line for line in app._lines)
        )

    async def test_approval_detail_renders_literally(self) -> None:
        """ApprovalScreen 中的命令/diff 必须按字面显示，防止 markup 隐藏/伪造。"""
        detail = (
            "命令：git log --oneline [conceal]secret[/conceal]\n"
            "--output=C:\\leak.txt [link=https://evil.example]点我[/link]"
        )
        app = self._make_app([])
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(ApprovalScreen("run_command", detail))
            for _ in range(50):
                await pilot.pause()
                if isinstance(app.screen, ApprovalScreen):
                    break
            self.assertIsInstance(app.screen, ApprovalScreen)
            widget = app.screen.query_one(".approval-detail", Static)
            content = widget.content
            plain = content.plain if hasattr(content, "plain") else str(content)
            self.assertIn("[conceal]secret[/conceal]", plain)
            self.assertIn("[link=https://evil.example]点我[/link]", plain)
            self.assertNotIn("secret", plain.replace("[conceal]secret[/conceal]", ""))
            await pilot.press("n")

    async def test_sidebar_session_name_renders_literally(self) -> None:
        """侧边栏中的会话名含 Rich 标签时必须按字面显示。"""
        response = ProviderResponse(
            content=None,
            tool_calls=(ToolCall("c1", "finish", {"summary": "ok"}),),
            finish_reason="tool_calls",
        )
        app = self._make_app([response])
        async with app.run_test() as pilot:
            await pilot.pause()
            app.runtime.create("[red]evil[/red]会话")
            app._refresh_sidebar_impl()
            content = app.query_one("#sidebar-content", Static).content
            plain = content.plain if hasattr(content, "plain") else str(content)
            self.assertIn("[red]evil[/red]会话", plain)


if __name__ == "__main__":
    unittest.main()
