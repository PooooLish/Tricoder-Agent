import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import (
    ProviderCompleted,
    TextDelta,
    ToolCallStarted,
    ToolCallCompleted,
    UsageReported,
)
from tricoder.models import Message, ProviderConfig, TokenUsage, ToolDefinition
from tricoder import providers as providers_module
from tricoder.providers import OpenAICompatibleProvider, ProviderError, ProviderProtocolError


def _event(payload: object) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode("utf-8")


class StreamingTransport:
    """只替代网络字节源，保留 Provider 的 SSE 与协议转换逻辑。"""

    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self.chunks = list(chunks)
        self.calls: list[dict[str, object]] = []

    async def post_stream(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
        cancellation: CancellationToken | None = None,
    ):
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def _provider(chunks: list[bytes | Exception]) -> tuple[OpenAICompatibleProvider, StreamingTransport]:
    transport = StreamingTransport(chunks)
    provider = OpenAICompatibleProvider(
        ProviderConfig("openai", "test-secret", "https://example.test/v1", "test-model"),
        transport=transport,  # type: ignore[arg-type]
        max_attempts=1,
    )
    return provider, transport


async def _collect(provider: OpenAICompatibleProvider, token: CancellationToken | None = None):
    return [event async for event in provider.stream([Message("user", "hello")], cancellation=token)]


class ProviderStreamingTests(unittest.TestCase):
    def test_cancellation_interrupts_retry_backoff(self) -> None:
        """连接失败后的退避等待必须可取消，不能让 UI 卡满整个重试周期。"""
        transport = StreamingTransport([ConnectionResetError("offline")])
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                "openai",
                "test-secret",
                "https://example.test/v1",
                "test-model",
            ),
            transport=transport,  # type: ignore[arg-type]
            max_attempts=2,
        )
        token = CancellationToken()
        timer = threading.Timer(0.1, token.cancel)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(CancellationError):
                asyncio.run(_collect(provider, token))
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.7)

    def test_stream_decodes_utf8_across_chunks_and_multiline_sse_data(self) -> None:
        """防止字节分片破坏中文，或多行 data 被当成多个 JSON 事件。"""
        raw = (
            'data: {"choices":[{"delta":{"content":"你\\n好"},\n'
            'data: "finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")
        split = raw.index("你".encode("utf-8")) + 1
        provider, transport = _provider([raw[:split], raw[split:]])

        events = asyncio.run(_collect(provider))

        self.assertEqual("你\n好", events[0].text)
        self.assertIsInstance(events[0], TextDelta)
        self.assertEqual("stop", events[1].finish_reason)
        self.assertIsInstance(events[1], ProviderCompleted)
        self.assertTrue(transport.calls[0]["payload"]["stream"])
        self.assertEqual({"include_usage": True}, transport.calls[0]["payload"]["stream_options"])

    def test_stream_assembles_tool_arguments_only_at_completed_boundary(self) -> None:
        """防止分片 JSON 参数在完整响应边界前成为可执行工具调用。"""
        chunks = [
            _event({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": '{"pa'}}]}, "finish_reason": None}]}),
            _event({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th":"README.md"}'}}]}, "finish_reason": None}]}),
            _event({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            _event("[DONE]"),
        ]
        provider, _ = _provider(chunks)

        events = asyncio.run(_collect(provider))

        calls = [event.call for event in events if isinstance(event, ToolCallCompleted)]
        starts = [event for event in events if isinstance(event, ToolCallStarted)]
        self.assertEqual([("call-1", "read_file")], [(event.call_id, event.name) for event in starts])
        self.assertEqual(1, len(calls))
        self.assertEqual("call-1", calls[0].id)
        self.assertEqual("read_file", calls[0].name)
        self.assertEqual({"path": "README.md"}, calls[0].arguments)

    def test_usage_precedes_provider_completion_after_finish_reason(self) -> None:
        """OpenAI 的尾部 usage 必须在统一完成事件前交付，不能被提前截断。"""
        provider, _ = _provider([
            _event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            _event({"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 3}}),
            _event("[DONE]"),
        ])

        events = asyncio.run(_collect(provider))

        self.assertEqual(
            [UsageReported, ProviderCompleted],
            [type(event) for event in events],
        )
        self.assertEqual("stop", events[-1].finish_reason)

    def test_stream_reports_usage_and_ignores_unknown_events(self) -> None:
        """防止未知扩展事件终止兼容流，或最终 usage 被遗漏。"""
        provider, _ = _provider([
            _event({"provider_extension": {"value": 1}}),
            _event({"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 5}}}),
            _event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            _event("[DONE]"),
        ])

        events = asyncio.run(_collect(provider))

        usage = [event.usage for event in events if isinstance(event, UsageReported)]
        self.assertEqual([TokenUsage(8, 3, 5, None)], usage)

    def test_stream_rejects_oversized_sse_event_without_echoing_body(self) -> None:
        """防止单事件无限增长，且错误消息不得包含响应正文。"""
        secret = "secret-response-body"
        provider, _ = _provider([_event({"unknown": secret})])

        with patch.object(providers_module, "_MAX_STREAM_EVENT_BYTES", 16):
            with self.assertRaises(ProviderProtocolError) as raised:
                asyncio.run(_collect(provider))

        self.assertNotIn(secret, str(raised.exception))

    def test_stream_rejects_cumulative_output_overflow(self) -> None:
        """防止大量合法小增量绕过累计输出内存上限。"""
        provider, _ = _provider([
            _event({"choices": [{"delta": {"content": "12345"}, "finish_reason": None}]}),
            _event({"choices": [{"delta": {"content": "67890"}, "finish_reason": None}]}),
        ])

        with patch.object(providers_module, "_MAX_STREAM_OUTPUT_BYTES", 8):
            with self.assertRaises(ProviderProtocolError):
                asyncio.run(_collect(provider))

    def test_stream_maps_connection_interruption_to_safe_provider_error(self) -> None:
        """防止网络异常原文穿过 Provider 安全错误边界。"""
        secret = "socket-secret"
        provider, _ = _provider([ConnectionResetError(secret)])

        with self.assertRaises(ProviderError) as raised:
            asyncio.run(_collect(provider))

        self.assertTrue(raised.exception.retryable)
        self.assertNotIn(secret, str(raised.exception))

    def test_stream_checks_cancellation_before_requesting_next_chunk(self) -> None:
        """防止 UI 取消后 Provider 仍继续读取后续网络内容。"""
        provider, _ = _provider([
            _event({"choices": [{"delta": {"content": "first"}, "finish_reason": None}]}),
            _event({"choices": [{"delta": {"content": "second"}, "finish_reason": None}]}),
        ])
        token = CancellationToken()

        async def exercise() -> None:
            stream = provider.stream([Message("user", "hello")], cancellation=token)
            first = await anext(stream)
            self.assertEqual("first", first.text)
            token.cancel()
            with self.assertRaises(CancellationError):
                await anext(stream)

        asyncio.run(exercise())

    def test_interrupted_incomplete_tool_call_never_becomes_executable(self) -> None:
        """防止连接中断时把半截工具参数提交给 Agent。"""
        provider, _ = _provider([
            _event({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}}]}, "finish_reason": None}]}),
            ConnectionResetError("interrupted"),
        ])

        observed: list[object] = []

        async def exercise() -> None:
            with self.assertRaises(ProviderError):
                async for event in provider.stream([Message("user", "hello")]):
                    observed.append(event)

        asyncio.run(exercise())
        self.assertFalse(any(isinstance(event, ToolCallCompleted) for event in observed))

    def test_stream_request_keeps_native_tool_schema(self) -> None:
        """防止流式入口遗漏同步入口已有的 structured tool 定义。"""
        provider, transport = _provider([
            _event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            _event("[DONE]"),
        ])
        tool = ToolDefinition("read_file", "读取", {"type": "object"})

        asyncio.run(_collect_with_tools(provider, (tool,)))

        payload = transport.calls[0]["payload"]
        self.assertEqual("auto", payload["tool_choice"])
        self.assertEqual("read_file", payload["tools"][0]["function"]["name"])


async def _collect_with_tools(provider: OpenAICompatibleProvider, tools: tuple[ToolDefinition, ...]):
    return [event async for event in provider.stream([Message("user", "hello")], tools)]


if __name__ == "__main__":
    unittest.main()
