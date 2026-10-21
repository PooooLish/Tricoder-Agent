"""流式等待预算与连接所有权的离线回归。"""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from test_provider_streaming import _event, _collect
from tricoder.core.cancellation import CancellationToken, CancellationError
from tricoder.core.events import ProviderCompleted, UsageReported, ToolCallCompleted
from tricoder.models import ProviderConfig
from tricoder.providers import OpenAICompatibleProvider, ProviderError, UrllibTransport
from tricoder.task_cleanup import TaskCleanup, cleanup_scope


class TimedTransport:
    def __init__(self, chunks, repeat=False):
        self.chunks, self.repeat = chunks, repeat
        self.closed = 0
        self.calls = 0

    async def post_stream(self, *args):
        self.calls += 1
        try:
            while True:
                for delay, chunk in self.chunks:
                    await asyncio.sleep(delay)
                    yield chunk
                if not self.repeat:
                    await asyncio.Event().wait()
        finally:
            self.closed += 1


def provider(transport, timeout=.08, attempts=1):
    return OpenAICompatibleProvider(
        ProviderConfig("openai", "offline", "https://example.invalid/v1", "fake"),
        transport=transport, timeout=timeout, max_attempts=attempts,
    )


class ProviderLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_retryable_connection_error_does_not_start_task_termination_budget(self):
        import urllib.error
        class Response:
            def read1(self, size):
                return _event("[DONE]")
            def close(self):
                time.sleep(.02)
        scope = TaskCleanup(budget=.1)
        with cleanup_scope(scope), patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("synthetic"), Response()]):
            events = await _collect(provider(UrllibTransport(), timeout=1, attempts=2))
        self.assertEqual(1, sum(isinstance(event, ProviderCompleted) for event in events))
        self.assertIsNone(scope.started_deadline)
        self.assertFalse(scope.failed)
        self.assertFalse(scope.has_pending)

    async def test_second_cancel_during_anext_cleanup_retains_owner(self):
        entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Transport:
            async def post_stream(self, *args):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closing.set()
                    await release.wait()
                yield b""
        instance = provider(Transport(), timeout=2)
        scope = TaskCleanup(budget=.5)
        with cleanup_scope(scope):
            task = asyncio.create_task(_collect(instance))
            await entered.wait()
            task.cancel()
            await closing.wait()
            task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(scope.has_pending)
                self.assertTrue(scope.failed)
                with self.assertRaises(ProviderError) as caught:
                    await _collect(instance)
                self.assertEqual("cleanup_failed", caught.exception.code)
            finally:
                release.set()
                await asyncio.sleep(.05)
            self.assertTrue(scope.retry(time.monotonic()+.1))

    async def test_urllib_worker_start_failure_leaves_no_live_resource(self):
        scope = TaskCleanup()
        with cleanup_scope(scope), patch("threading.Thread.start", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                await _collect(provider(UrllibTransport()))
        self.assertFalse(scope.has_pending)

    async def test_done_at_every_byte_boundary(self):
        data = _event({"choices": [{"delta": {"content": "中文"}, "finish_reason": "stop"}]}) + _event("[DONE]")
        transport = TimedTransport([(0, bytes([byte])) for byte in data])
        events = await asyncio.wait_for(_collect(provider(transport, timeout=2)), 8)
        self.assertEqual(1, sum(isinstance(e, ProviderCompleted) for e in events))

    async def test_blocked_read_is_released_by_close_on_async_cancel(self):
        reading, release, closed = threading.Event(), threading.Event(), threading.Event()
        class Response:
            def read1(self, size):
                reading.set()
                release.wait(2)
                return b""
            def close(self):
                release.set()
                closed.set()
        scope = TaskCleanup(budget=.3)
        with patch("urllib.request.urlopen", return_value=Response()), cleanup_scope(scope):
            task = asyncio.create_task(_collect(provider(UrllibTransport(), timeout=1)))
            while not reading.is_set():
                await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
            self.assertFalse(scope.has_pending)

    async def test_blocked_close_retains_resource_and_refuses_next_request(self):
        release = threading.Event()
        class Response:
            def read1(self, size):
                return _event("[DONE]")
            def close(self):
                release.wait(2)
        scope = TaskCleanup(budget=.04)
        # 共用预先开启的终止预算，避免每层关闭都重新获得时间。
        scope.begin_termination()
        instance = provider(UrllibTransport())
        with patch("urllib.request.urlopen", return_value=Response()), cleanup_scope(scope):
            try:
                with self.assertRaises(ProviderError) as caught:
                    await _collect(instance)
                self.assertTrue(scope.failed, str(caught.exception))
                self.assertTrue(scope.has_pending)
                with self.assertRaises(ProviderError) as caught:
                    await _collect(instance)
                self.assertEqual("cleanup_failed", caught.exception.code)
            finally:
                release.set()
                await asyncio.sleep(.05)
            self.assertTrue(scope.retry(time.monotonic() + .2))

    async def test_done_does_not_wait_for_eof_and_preserves_usage(self):
        transport = TimedTransport([(0, _event({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]})),
                                    (0, _event({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}) + _event("[DONE]"))])
        events = await asyncio.wait_for(_collect(provider(transport)), .6)
        self.assertEqual(1, sum(isinstance(e, ProviderCompleted) for e in events))
        self.assertEqual(1, sum(isinstance(e, UsageReported) for e in events))
        self.assertEqual(1, transport.closed)

    async def test_heartbeats_and_usage_do_not_extend_first_progress(self):
        for chunk in (b": ping\n\n", _event({"choices": [], "usage": {"prompt_tokens": 1}})):
            with self.subTest(chunk=chunk):
                transport = TimedTransport([(.01, chunk)], repeat=True)
                with self.assertRaises(ProviderError) as caught:
                    await asyncio.wait_for(_collect(provider(transport)), .6)
                self.assertEqual("first_progress_timeout", caught.exception.code)
                self.assertEqual(1, transport.closed)

    async def test_text_then_idle_times_out(self):
        transport = TimedTransport([(0, _event({"choices": [{"delta": {"content": "x"}}]}))])
        with self.assertRaises(ProviderError) as caught:
            await asyncio.wait_for(_collect(provider(transport)), .6)
        self.assertEqual("idle_timeout", caught.exception.code)
        self.assertEqual(1, transport.calls)

    async def test_tool_argument_growth_is_progress(self):
        chunks = [(0, _event({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "finish", "arguments": "{"}}]}}]}))]
        for value in ('"summary"', ':', '"ok"', '}'):
            chunks.append((.1, _event({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": value}}]}}]})))
        chunks.append((0, _event("[DONE]")))
        # 累计参数耗时超过 T，但每段间隔小于 T；留出 Windows/TUI 测试并发调度余量。
        events = await asyncio.wait_for(_collect(provider(TimedTransport(chunks), timeout=.3)), 1.5)
        self.assertEqual(1, sum(isinstance(e, ToolCallCompleted) for e in events))

    async def test_continuous_progress_has_total_deadline(self):
        transport = TimedTransport([(.02, _event({"choices": [{"delta": {"reasoning_content": "x"}}]}))], repeat=True)
        with self.assertRaises(ProviderError) as caught:
            await asyncio.wait_for(_collect(provider(transport)), .7)
        self.assertEqual("request_deadline_exceeded", caught.exception.code)

    async def test_retry_backoff_cannot_outlive_total_budget(self):
        class Broken:
            async def post_stream(self, *args):
                raise ProviderError("offline", retryable=True)
                yield b""
        with self.assertRaises(ProviderError) as caught:
            await asyncio.wait_for(_collect(provider(Broken(), attempts=3)), .7)
        self.assertEqual("request_deadline_exceeded", caught.exception.code)

    async def test_late_urlopen_response_is_closed_and_owner_retained(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        class Response:
            def read1(self, size):
                return b""
            def close(self):
                closed.set()
        def open_response(*args, **kwargs):
            entered.set()
            release.wait(2)
            return Response()
        scope = TaskCleanup(budget=.04)
        transport = UrllibTransport()
        token = CancellationToken()
        with patch("urllib.request.urlopen", open_response), cleanup_scope(scope):
            task = asyncio.create_task(_collect(provider(transport), token))
            try:
                while not entered.is_set():
                    await asyncio.sleep(.005)
                token.cancel()
                with self.assertRaises(CancellationError):
                    await asyncio.wait_for(task, .7)
                self.assertTrue(scope.failed)
                self.assertTrue(scope.has_pending)
            finally:
                release.set()
                await asyncio.sleep(.05)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(closed.is_set())
            self.assertTrue(scope.retry(time.monotonic() + .2))
