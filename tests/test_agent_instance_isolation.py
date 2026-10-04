from __future__ import annotations

import asyncio
import unittest

from tricoder.agent import CodingAgent
from tricoder.context.memory import ConversationMemory, MemoryItem
from tricoder.context.summarizer import MemorySummaryResult
from tricoder.core.cancellation import CancellationToken
from tricoder.models import (
    MemoryConfig,
    ProviderResponse,
    SessionContext,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class _FinishProvider:
    def __init__(self, label: str, usage: TokenUsage) -> None:
        self.label = label
        self.usage = usage
        self.requests: list[tuple[object, ...]] = []

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.requests.append(tuple(messages))
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    f"finish-{self.label}",
                    "finish",
                    {"summary": f"完成-{self.label}"},
                ),
            ),
            finish_reason="tool_calls",
            usage=self.usage,
        )


class _FinishTools:
    definitions = (ToolDefinition("finish", "结束", {"type": "object"}),)

    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def contains(name: str) -> bool:
        return name == "finish"

    @staticmethod
    def describe(name: str):  # type: ignore[no-untyped-def]
        return _FinishTools.definitions[0] if name == "finish" else None

    @staticmethod
    def requires_approval(name: str) -> bool:
        return False

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        self.calls.append(name)
        return ToolResult(True, str(arguments.get("summary", "")))


class _Observer:
    def __init__(self) -> None:
        self.rounds: list[int] = []
        self.usages: list[tuple[int, TokenUsage]] = []
        self.errors: list[str] = []

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        self.rounds.append(round_number)

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
        self.usages.append((round_number, usage))

    def on_action(self, action: object) -> None:
        return None

    def on_tool_result(
        self,
        action: object,
        result: object,
        duration_ms: int,
    ) -> None:
        return None

    def on_error(self, message: str) -> None:
        self.errors.append(message)


class _ControlledSummarizer:
    def __init__(
        self,
        label: str,
        usage: TokenUsage,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.label = label
        self.usage = usage
        self.entered = entered
        self.release = release
        self.sources: list[tuple[object, ...]] = []

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.sources.append(captured)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        cancellation.raise_if_cancelled()
        numbered = [message for message in captured if message.message_seq is not None]
        task_message = next(message for message in numbered if message.kind == "task")
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=max(message.message_seq or 0 for message in numbered),
                goal=MemoryItem(
                    f"goal-{self.label}",
                    f"只属于 {self.label} 的目标",
                    (f"m{task_message.message_seq}",),
                    "task",
                    task_message.task_id,
                ),
            ),
            self.usage,
        )


def _agent(
    label: str,
    business_usage: TokenUsage,
    memory_usage: TokenUsage,
    *,
    entered: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> tuple[CodingAgent, _FinishProvider, _FinishTools, _Observer, _ControlledSummarizer]:
    provider = _FinishProvider(label, business_usage)
    tools = _FinishTools()
    observer = _Observer()
    summarizer = _ControlledSummarizer(
        label,
        memory_usage,
        entered=entered,
        release=release,
    )
    agent = CodingAgent(
        provider,  # type: ignore[arg-type]
        tools,  # type: ignore[arg-type]
        plan_enabled=False,
        max_rounds=1,
        max_context_chars=100_000,
        observer=observer,  # type: ignore[arg-type]
        memory_config=MemoryConfig(
            compaction="structured",
            persistence="reviewed_summary",
        ),
        memory_summarizer=summarizer,
    )
    return agent, provider, tools, observer, summarizer


class AgentInstanceIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_agents_keep_messages_candidates_usage_and_tools_isolated(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        original_a = SessionContext()
        original_b = SessionContext()
        agent_a, provider_a, tools_a, observer_a, summarizer_a = _agent(
            "A",
            TokenUsage(101, 11),
            TokenUsage(31, 3),
            entered=entered,
            release=release,
        )
        agent_b, provider_b, tools_b, observer_b, summarizer_b = _agent(
            "B",
            TokenUsage(202, 22),
            TokenUsage(42, 4),
        )
        self.assertIsNot(agent_a.context_manager, agent_b.context_manager)
        task_a = asyncio.create_task(
            agent_a.run_with_context_async("任务-A", original_a)
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            turn_b = await asyncio.wait_for(
                agent_b.run_with_context_async("任务-B", original_b),
                2,
            )
            self.assertFalse(task_a.done())
            release.set()
            turn_a = await asyncio.wait_for(task_a, 2)
        finally:
            release.set()
            if not task_a.done():
                task_a.cancel()
            await asyncio.wait_for(
                asyncio.gather(task_a, return_exceptions=True),
                2,
            )

        self.assertTrue(turn_a.result.ok)
        self.assertTrue(turn_b.result.ok)
        self.assertEqual(["finish"], tools_a.calls)
        self.assertEqual(["finish"], tools_b.calls)
        self.assertEqual(1, turn_a.result.tool_calls)
        self.assertEqual(1, turn_b.result.tool_calls)
        self.assertEqual(TokenUsage(101, 11), turn_a.result.usage)
        self.assertEqual(TokenUsage(202, 22), turn_b.result.usage)
        self.assertEqual(TokenUsage(31, 3), turn_a.memory_usage)
        self.assertEqual(TokenUsage(42, 4), turn_b.memory_usage)
        self.assertEqual(1, turn_a.memory_calls)
        self.assertEqual(1, turn_b.memory_calls)
        self.assertEqual("goal-A", turn_a.context.review_memory_candidate.goal.id)
        self.assertEqual("goal-B", turn_b.context.review_memory_candidate.goal.id)
        self.assertEqual(
            ["finish-A"],
            [
                call.id
                for message in turn_a.context.messages
                for call in message.tool_calls
            ],
        )
        self.assertEqual(
            ["finish-B"],
            [
                call.id
                for message in turn_b.context.messages
                for call in message.tool_calls
            ],
        )
        self.assertTrue(
            all("任务-B" not in (message.content or "") for message in turn_a.context.messages)
        )
        self.assertTrue(
            all("任务-A" not in (message.content or "") for message in turn_b.context.messages)
        )
        self.assertTrue(
            all("任务-B" not in (message.content or "") for message in provider_a.requests[0])
        )
        self.assertTrue(
            all("任务-A" not in (message.content or "") for message in provider_b.requests[0])
        )
        self.assertEqual([TokenUsage(101, 11)], [usage for _, usage in observer_a.usages])
        self.assertEqual([TokenUsage(202, 22)], [usage for _, usage in observer_b.usages])
        self.assertTrue(
            all(
                "任务-B" not in (message.content or "")
                for source in summarizer_a.sources
                for message in source
            )
        )
        self.assertTrue(
            all(
                "任务-A" not in (message.content or "")
                for source in summarizer_b.sources
                for message in source
            )
        )
        self.assertEqual(SessionContext(), original_a)
        self.assertEqual(SessionContext(), original_b)

    async def test_explicit_parent_cancellation_of_a_does_not_affect_b(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        parent_a = CancellationToken()
        parent_b = CancellationToken()
        agent_a, _provider_a, tools_a, _observer_a, _summarizer_a = _agent(
            "A-cancel",
            TokenUsage(111, 11),
            TokenUsage(33, 3),
            entered=entered,
            release=release,
        )
        agent_b, _provider_b, tools_b, _observer_b, _summarizer_b = _agent(
            "B-live",
            TokenUsage(222, 22),
            TokenUsage(44, 4),
        )
        task_a = asyncio.create_task(
            agent_a.run_with_context_async(
                "任务-A-取消",
                SessionContext(),
                cancellation=parent_a,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(parent_a.cancel())
            turn_b = await asyncio.wait_for(
                agent_b.run_with_context_async(
                    "任务-B-继续",
                    SessionContext(),
                    cancellation=parent_b,
                ),
                2,
            )
            release.set()
            turn_a = await asyncio.wait_for(task_a, 2)
        finally:
            release.set()
            if not task_a.done():
                task_a.cancel()
            await asyncio.wait_for(
                asyncio.gather(task_a, return_exceptions=True),
                2,
            )

        self.assertFalse(turn_a.result.ok)
        self.assertEqual("任务已取消", turn_a.result.summary)
        self.assertTrue(parent_a.is_cancelled)
        self.assertFalse(parent_b.is_cancelled)
        self.assertTrue(turn_b.result.ok)
        self.assertEqual("goal-B-live", turn_b.context.review_memory_candidate.goal.id)
        self.assertIsNone(turn_a.context.review_memory_candidate)
        self.assertEqual(["finish"], tools_a.calls)
        self.assertEqual(["finish"], tools_b.calls)

    async def test_task_cancel_of_a_does_not_cancel_parent_or_agent_b(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        parent_a = CancellationToken()
        parent_b = CancellationToken()
        agent_a, _provider_a, tools_a, _observer_a, _summarizer_a = _agent(
            "A-native",
            TokenUsage(121, 12),
            TokenUsage(35, 3),
            entered=entered,
            release=release,
        )
        agent_b, _provider_b, tools_b, _observer_b, _summarizer_b = _agent(
            "B-native",
            TokenUsage(242, 24),
            TokenUsage(46, 4),
        )
        task_a = asyncio.create_task(
            agent_a.run_with_context_async(
                "任务-A-原生取消",
                SessionContext(),
                cancellation=parent_a,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            turn_b = await asyncio.wait_for(
                agent_b.run_with_context_async(
                    "任务-B-不取消",
                    SessionContext(),
                    cancellation=parent_b,
                ),
                2,
            )
            task_a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task_a, 2)
        finally:
            release.set()
            if not task_a.done():
                task_a.cancel()
            await asyncio.wait_for(
                asyncio.gather(task_a, return_exceptions=True),
                2,
            )

        self.assertFalse(parent_a.is_cancelled)
        self.assertFalse(parent_b.is_cancelled)
        self.assertTrue(turn_b.result.ok)
        self.assertEqual("goal-B-native", turn_b.context.review_memory_candidate.goal.id)
        self.assertEqual(["finish"], tools_a.calls)
        self.assertEqual(["finish"], tools_b.calls)


if __name__ == "__main__":
    unittest.main()
