import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import (
    ApprovalRequested,
    ProviderCompleted,
    RoundStarted,
    RuntimeCompleted,
    RuntimeFailed,
    TextDelta,
    ToolCallCompleted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    UsageReported,
)
from tricoder.models import (
    Message,
    ProviderResponse,
    SessionContext,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class HybridProvider:
    """同步和流式入口表达同一组确定性响应。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        return self.responses.pop(0)

    async def stream(self, messages, tools=(), *, cancellation=None):  # type: ignore[no-untyped-def]
        response = self.responses.pop(0)
        if response.content:
            yield TextDelta(response.content)
        for call in response.tool_calls:
            yield ToolCallCompleted(call)
        if response.usage is not None:
            yield UsageReported(response.usage)
        yield ProviderCompleted(response.finish_reason)


class AsyncRegistry:
    """保留 Agent 工具边界的轻量测试实现，不模拟网络或文件系统。"""

    definitions: tuple[ToolDefinition, ...] = ()

    def __init__(self, *, cancel_after_first: CancellationToken | None = None) -> None:
        self.calls: list[str] = []
        self.cancel_after_first = cancel_after_first

    def contains(self, name: str) -> bool:
        return name in {"inspect", "finish"}

    def describe(self, name: str) -> ToolDefinition | None:
        if not self.contains(name):
            return None
        return ToolDefinition(name, "测试工具", {"type": "object"})

    def requires_approval(self, name: str) -> bool:
        return name == "inspect"

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        self.calls.append(name)
        return ToolResult(True, str(arguments.get("summary", "inspected")))

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        result = self.execute(name, arguments)
        if self.cancel_after_first is not None and len(self.calls) == 1:
            self.cancel_after_first.cancel()
        return result


def _finish_response(summary: str = "done") -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(ToolCall("finish-1", "finish", {"summary": summary}),),
        finish_reason="tool_calls",
        usage=TokenUsage(8, 2, 3, None),
    )


class AsyncAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_planning_returns_cancelled_result(self) -> None:
        """规划流被取消时应正常收尾，而不是把 CancellationError 泄漏给宿主。"""
        token = CancellationToken()

        class PlanningCancellationProvider:
            async def stream(self, messages, tools=(), *, cancellation=None):
                token.cancel()
                cancellation.raise_if_cancelled()
                if False:
                    yield ProviderCompleted("stop")

        agent = CodingAgent(
            PlanningCancellationProvider(),  # type: ignore[arg-type]
            AsyncRegistry(),  # type: ignore[arg-type]
        )
        observed: list[object] = []

        turn = await agent.run_with_context_async(
            "task",
            SessionContext(),
            cancellation=token,
            event_sink=observed.append,
        )

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(0, turn.result.rounds)
        self.assertIsInstance(observed[-1], RuntimeFailed)
        self.assertEqual("cancelled", observed[-1].category)

    async def test_sync_and_async_entrypoints_return_equivalent_results_and_audit_categories(self) -> None:
        """防止兼容包装与规范异步实现产生不同结果或审计状态。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            async_audit = root / "async.jsonl"
            sync_audit = root / "sync.jsonl"
            async_agent = CodingAgent(
                HybridProvider([_finish_response()]),
                AsyncRegistry(),  # type: ignore[arg-type]
                plan_enabled=False,
                audit=AuditLogger(async_audit),
            )
            sync_agent = CodingAgent(
                HybridProvider([_finish_response()]),
                AsyncRegistry(),  # type: ignore[arg-type]
                plan_enabled=False,
                audit=AuditLogger(sync_audit),
            )

            async_turn = await async_agent.run_with_context_async("task", SessionContext())
            sync_turn = await asyncio.to_thread(sync_agent.run_with_context, "task", SessionContext())

            self.assertEqual(async_turn, sync_turn)
            async_statuses = [json.loads(line)["status"] for line in async_audit.read_text(encoding="utf-8").splitlines()]
            sync_statuses = [json.loads(line)["status"] for line in sync_audit.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(async_statuses, sync_statuses)

    async def test_typed_events_follow_provider_policy_tool_and_completion_order(self) -> None:
        """防止工具在完整调用、审批事件或执行开始事件之前运行。"""
        call = ToolCall("inspect-1", "inspect", {})
        provider = HybridProvider([
            ProviderResponse(content="checking", tool_calls=(call,), finish_reason="tool_calls", usage=TokenUsage(4, 1)),
            _finish_response(),
        ])
        registry = AsyncRegistry()
        observed: list[object] = []
        agent = CodingAgent(provider, registry, plan_enabled=False)  # type: ignore[arg-type]

        turn = await agent.run_with_context_async("task", SessionContext(), event_sink=observed.append)

        self.assertTrue(turn.result.ok)
        event_types = [type(event) for event in observed]
        expected_in_order = [
            RoundStarted,
            TextDelta,
            ToolCallCompleted,
            UsageReported,
            ProviderCompleted,
            ApprovalRequested,
            ToolExecutionStarted,
            ToolExecutionCompleted,
            RoundStarted,
            ToolCallCompleted,
            UsageReported,
            ProviderCompleted,
            ToolExecutionStarted,
            ToolExecutionCompleted,
            RuntimeCompleted,
        ]
        self.assertEqual(expected_in_order, event_types)
        self.assertEqual(["inspect", "finish"], registry.calls)

    async def test_cancellation_between_tool_calls_completes_the_protocol_round(self) -> None:
        """防止取消在多工具中间留下孤立 assistant 调用或执行后续工具。"""
        token = CancellationToken()
        calls = (
            ToolCall("inspect-1", "inspect", {}),
            ToolCall("finish-1", "finish", {"summary": "must-not-run"}),
        )
        registry = AsyncRegistry(cancel_after_first=token)
        agent = CodingAgent(
            HybridProvider([ProviderResponse(tool_calls=calls, finish_reason="tool_calls")]),
            registry,  # type: ignore[arg-type]
            plan_enabled=False,
        )
        observed: list[object] = []

        turn = await agent.run_with_context_async(
            "task",
            SessionContext(),
            cancellation=token,
            event_sink=observed.append,
        )

        self.assertFalse(turn.result.ok)
        self.assertEqual(["inspect"], registry.calls)
        self.assertEqual(["assistant", "tool", "tool"], [message.role for message in turn.context.messages[-3:]])
        self.assertIsInstance(observed[-1], RuntimeFailed)

    async def test_sync_entrypoint_rejects_nested_event_loop(self) -> None:
        """防止同步包装在已有事件循环中调用 asyncio.run。"""
        agent = CodingAgent(HybridProvider([_finish_response()]), AsyncRegistry(), plan_enabled=False)  # type: ignore[arg-type]

        with self.assertRaises(RuntimeError):
            agent.run_with_context("task", SessionContext())


if __name__ == "__main__":
    unittest.main()
