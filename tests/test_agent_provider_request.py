from __future__ import annotations

import unittest

from tricoder.agent import CodingAgent
from tricoder.core.events import ProviderCompleted, ToolCallCompleted
from tricoder.models import SessionContext, ToolCall, ToolDefinition, ToolResult


class _StreamProvider:
    def __init__(
        self,
        events: tuple[object, ...],
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.events = events
        self.failure = failure
        self.stream_calls = 0
        self.complete_calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.complete_calls += 1
        raise AssertionError("流式失败不得降级到 complete")

    async def stream(self, messages, tools=(), *, cancellation=None):  # type: ignore[no-untyped-def]
        self.stream_calls += 1
        for event in self.events:
            yield event
        if self.failure is not None:
            raise self.failure


class _RecordingTools:
    definitions = (ToolDefinition("finish", "结束", {"type": "object"}),)

    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def contains(name: str) -> bool:
        return name == "finish"

    @staticmethod
    def describe(name: str):  # type: ignore[no-untyped-def]
        return _RecordingTools.definitions[0] if name == "finish" else None

    @staticmethod
    def requires_approval(name: str) -> bool:
        return False

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        self.calls.append(name)
        return ToolResult(True, "unexpected")


def _agent(provider: _StreamProvider, tools: _RecordingTools) -> CodingAgent:
    return CodingAgent(
        provider,  # type: ignore[arg-type]
        tools,  # type: ignore[arg-type]
        plan_enabled=False,
        max_rounds=1,
    )


class AgentProviderRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_then_stream_failure_propagates_same_exception_without_execution(self) -> None:
        call = ToolCall("finish-stream-break", "finish", {"summary": "no"})
        sentinel = RuntimeError("stream sentinel")
        provider = _StreamProvider(
            (ToolCallCompleted(call),),
            failure=sentinel,
        )
        tools = _RecordingTools()

        with self.assertRaises(RuntimeError) as captured:
            await _agent(provider, tools).run_with_context_async(
                "stream break",
                SessionContext(),
            )

        self.assertIs(sentinel, captured.exception)
        self.assertEqual([], tools.calls)
        self.assertEqual(0, provider.complete_calls)
        self.assertEqual(1, provider.stream_calls)

    async def test_missing_provider_completed_is_protocol_failure_not_partial_execution(self) -> None:
        call = ToolCall("finish-missing-complete", "finish", {"summary": "no"})
        provider = _StreamProvider((ToolCallCompleted(call),))
        tools = _RecordingTools()

        turn = await _agent(provider, tools).run_with_context_async(
            "missing completion",
            SessionContext(),
        )

        self.assertFalse(turn.result.ok)
        self.assertIn("达到最大轮数", turn.result.summary)
        self.assertEqual(0, turn.result.tool_calls)
        self.assertEqual([], tools.calls)
        self.assertEqual(0, provider.complete_calls)
        self.assertEqual(1, provider.stream_calls)
        self.assertFalse(any(message.role == "tool" for message in turn.context.messages))

    async def test_event_sink_exception_keeps_identity_and_prevents_tool_execution(self) -> None:
        call = ToolCall("finish-sink-break", "finish", {"summary": "no"})
        provider = _StreamProvider(
            (
                ToolCallCompleted(call),
                ProviderCompleted("tool_calls"),
            )
        )
        tools = _RecordingTools()
        sentinel = LookupError("event sink sentinel")

        def sink(event: object) -> None:
            if isinstance(event, ToolCallCompleted):
                raise sentinel

        with self.assertRaises(LookupError) as captured:
            await _agent(provider, tools).run_with_context_async(
                "sink break",
                SessionContext(),
                event_sink=sink,
            )

        self.assertIs(sentinel, captured.exception)
        self.assertEqual([], tools.calls)
        self.assertEqual(0, provider.complete_calls)
        self.assertEqual(1, provider.stream_calls)


if __name__ == "__main__":
    unittest.main()
