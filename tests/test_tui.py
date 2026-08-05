"""Textual TUI 的启动、任务流与审批模态测试。"""

import tempfile
import unittest
from pathlib import Path

from textual.widgets import Input, RichLog

from tricoder.models import ProviderConfig, ProviderResponse, ToolCall
from tricoder.session_runtime import RuntimeOptions, SessionRuntime
from tricoder.sessions import SessionStore
from tricoder.tui import ApprovalScreen, TricoderApp


class FakeProvider:
    """按序返回预设响应，不访问网络。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(
        self,
        messages: object,
        tools: tuple = (),
    ) -> ProviderResponse:
        self.calls += 1
        return self._responses.pop(0)


def _provider_factory(responses: list[ProviderResponse]):
    provider = FakeProvider(responses)

    def build(config: ProviderConfig, timeout: float) -> FakeProvider:
        return provider

    return build


class TricoderTuiTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertIsNotNone(app.query_one("#log", RichLog))

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


if __name__ == "__main__":
    unittest.main()
