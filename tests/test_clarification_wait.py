"""澄清等待对象的首次完成、取消和迟到答案竞态。"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from io import StringIO

from rich.console import Console
from unittest import mock
from types import SimpleNamespace

from tricoder.core.cancellation import CancellationToken
from tricoder.core.clarification import ClarificationStatus
from tricoder.presentation.clarification_wait import ClarificationWait
from tricoder.presentation.clarification_wait import read_console_clarification
from tricoder.presentation.console import TerminalUI
from tricoder.presentation.tui import ClarificationScreen, TricoderApp
from tricoder.core.clarification import ClarificationRequest, ClarificationResult


class ClarificationWaitTests(unittest.TestCase):
    def test_answer_preserves_free_text_and_slash_command_literal(self) -> None:
        wait = ClarificationWait()
        self.assertTrue(wait.answer("/clear"))
        result = wait.wait(CancellationToken(), timeout=0.1)
        self.assertEqual(ClarificationStatus.ANSWERED, result.status)
        self.assertEqual("/clear", result.answer)

    def test_empty_and_oversized_answers_do_not_complete(self) -> None:
        wait = ClarificationWait()
        self.assertFalse(wait.answer("   "))
        self.assertFalse(wait.answer("x" * 4001))
        result = wait.wait(CancellationToken(), timeout=0.01)
        self.assertEqual(ClarificationStatus.TIMED_OUT, result.status)

    def test_cancel_wins_and_late_answer_cannot_revive_wait(self) -> None:
        wait = ClarificationWait()
        self.assertTrue(wait.cancel())
        self.assertFalse(wait.answer("late"))
        result = wait.wait(CancellationToken(), timeout=0.1)
        self.assertEqual(ClarificationStatus.CANCELLED, result.status)
        self.assertIsNone(result.answer)

    def test_token_cancellation_releases_waiter(self) -> None:
        wait = ClarificationWait()
        token = CancellationToken()
        results = []
        worker = threading.Thread(
            target=lambda: results.append(wait.wait(token, timeout=10)), daemon=True
        )
        worker.start()
        time.sleep(0.02)
        token.cancel()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(ClarificationStatus.CANCELLED, results[0].status)
        self.assertFalse(wait.answer("late"))

    def test_unavailable_is_a_distinct_terminal_state(self) -> None:
        wait = ClarificationWait()
        self.assertTrue(wait.unavailable("noninteractive"))
        result = wait.wait(CancellationToken(), timeout=0.1)
        self.assertEqual(ClarificationStatus.UNAVAILABLE, result.status)
        self.assertEqual("noninteractive", result.reason)

    def test_noninteractive_console_never_starts_a_background_reader(self) -> None:
        source = StringIO("an answer\n")
        result = read_console_clarification(
            CancellationToken(),
            0.1,
            input_stream=source,
            output_stream=StringIO(),
        )
        self.assertEqual(ClarificationStatus.UNAVAILABLE, result.status)
        self.assertEqual("noninteractive", result.reason)

    def test_terminal_ui_supports_options_and_free_text_reader(self) -> None:
        output = StringIO()
        seen = []

        def reader(request, cancellation, timeout):  # type: ignore[no-untyped-def]
            seen.append((request, cancellation, timeout))
            return ClarificationResult.answered("自由文本，不是默认选项")

        ui = TerminalUI(
            console=Console(file=output, force_terminal=False, width=100),
            clarification_reader=reader,
        )
        request = ClarificationRequest("request-1", "选择哪一种？", ("A", "B"))
        token = CancellationToken()

        result = ui.ask_user(request, token, 12.0)

        self.assertEqual(ClarificationStatus.ANSWERED, result.status)
        self.assertEqual("自由文本，不是默认选项", result.answer)
        self.assertEqual([(request, token, 12.0)], seen)
        rendered = output.getvalue()
        self.assertIn("选择哪一种", rendered)
        self.assertIn("1. A", rendered)
        self.assertIn("自由文本", rendered)

    def test_custom_blocking_input_without_reader_is_unavailable(self) -> None:
        called = False

        def unsafe_input(_prompt):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return "answer"

        ui = TerminalUI(
            console=Console(file=StringIO(), force_terminal=False),
            input_fn=unsafe_input,
        )
        result = ui.ask_user(
            ClarificationRequest("request-2", "问题"),
            CancellationToken(),
            1.0,
        )
        self.assertEqual(ClarificationStatus.UNAVAILABLE, result.status)
        self.assertFalse(called)


class TuiClarificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tui_screen_submits_literal_slash_text(self) -> None:
        runtime = mock.Mock()
        runtime.status.return_value = SimpleNamespace(
            record=None,
            provider="openai",
            model="synthetic",
            workspace="workspace",
            read_only=False,
            verification="未运行",
            modified_files=0,
            modified_directories=0,
        )
        runtime.permission_level = "strict"
        runtime.request_shutdown.return_value = False
        runtime.cancel_current.return_value = False
        runtime.cleanup_pending_resources.return_value = True
        runtime.close.return_value = True
        app = TricoderApp(lambda *_: runtime)
        results: list[ClarificationResult] = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(
                ClarificationScreen(
                    ClarificationRequest("screen-request", "请输入答案", ("A", "B")),
                    5.0,
                ),
                results.append,
            )
            await pilot.pause()
            field = app.screen.query_one("#clarification-input")
            field.value = "/clear"
            await pilot.press("enter")
            await pilot.pause()

        self.assertEqual(1, len(results))
        self.assertEqual(ClarificationStatus.ANSWERED, results[0].status)
        self.assertEqual("/clear", results[0].answer)

    async def test_tui_clarifier_returns_modal_free_text(self) -> None:
        app = TricoderApp(lambda *_: None)
        token = CancellationToken()
        request = ClarificationRequest("tui-request", "请输入分支名", ("main", "dev"))
        loop = asyncio.get_running_loop()
        tasks: list[asyncio.Task[object]] = []

        async def screen(_screen):  # type: ignore[no-untyped-def]
            return ClarificationResult.answered("feature/demo")

        def dispatch(callback):  # type: ignore[no-untyped-def]
            loop.call_soon_threadsafe(callback)

        def run_worker(worker, **_kwargs):  # type: ignore[no-untyped-def]
            task = asyncio.create_task(worker())
            tasks.append(task)

        with mock.patch.object(app, "push_screen_wait", side_effect=screen), \
             mock.patch.object(app, "call_from_thread", side_effect=dispatch), \
             mock.patch.object(app, "run_worker", side_effect=run_worker), \
             mock.patch.object(app, "_dismiss_clarification"):
            result = await asyncio.to_thread(app._clarifier, request, token, 1.0)
            await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual(ClarificationStatus.ANSWERED, result.status)
        self.assertEqual("feature/demo", result.answer)

    async def test_tui_cancel_releases_wait_and_late_answer_is_ignored(self) -> None:
        app = TricoderApp(lambda *_: None)
        token = CancellationToken()
        request = ClarificationRequest("cancel-request", "等待取消")
        entered = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()
        tasks: list[asyncio.Task[object]] = []

        async def screen(_screen):  # type: ignore[no-untyped-def]
            entered.set()
            await release.wait()
            return ClarificationResult.answered("late")

        def dispatch(callback):  # type: ignore[no-untyped-def]
            loop.call_soon_threadsafe(callback)

        def run_worker(worker, **_kwargs):  # type: ignore[no-untyped-def]
            task = asyncio.create_task(worker())
            tasks.append(task)

        with mock.patch.object(app, "push_screen_wait", side_effect=screen), \
             mock.patch.object(app, "call_from_thread", side_effect=dispatch), \
             mock.patch.object(app, "run_worker", side_effect=run_worker), \
             mock.patch.object(app, "_dismiss_clarification"):
            result_task = asyncio.create_task(
                asyncio.to_thread(app._clarifier, request, token, 10.0)
            )
            await asyncio.wait_for(entered.wait(), 1)
            token.cancel()
            result = await asyncio.wait_for(result_task, 1)
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual(ClarificationStatus.CANCELLED, result.status)


if __name__ == "__main__":
    unittest.main()
