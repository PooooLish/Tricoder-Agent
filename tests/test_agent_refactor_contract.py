"""Agent 第一轮整理的行为契约。

这些测试只固定重构前已经存在的公开行为和关键时序，不为新模块预设实现细节。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tricoder.agent import CodingAgent, _complete_round_tail
from tricoder.core.events import (
    ProviderCompleted,
    RoundStarted,
    RuntimeCompleted,
    ToolCallCompleted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from tricoder.models import MemoryConfig, ProviderResponse, SessionContext, ToolCall
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ProviderError
from tricoder.tools import ToolContext
from tests.test_agent import CallIdRecordingRegistry, StructuredScriptedProvider


class AgentRefactorContractTests(unittest.TestCase):
    """重构必须保持的最小端到端指纹。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.registry = CallIdRecordingRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                lambda *_request: True,
            )
        )

    def test_success_keeps_public_result_pairing_and_event_order(self) -> None:
        call = ToolCall("finish-contract", "finish", {"summary": "done"})
        provider = StructuredScriptedProvider(
            [ProviderResponse(tool_calls=(call,), finish_reason="tool_calls")]
        )
        events: list[object] = []

        turn = CodingAgent(
            provider,
            self.registry,
            memory_config=MemoryConfig(compaction="off", persistence="off"),
            plan_enabled=False,
            max_rounds=1,
        ).run_with_context("contract", SessionContext(), event_sink=events.append)

        self.assertTrue(turn.result.ok)
        self.assertEqual("done", turn.result.summary)
        self.assertEqual(1, turn.result.rounds)
        self.assertEqual(1, turn.result.tool_calls)
        self.assertEqual(["finish-contract"], self.registry.call_ids)
        assistant_index = next(
            index
            for index, message in enumerate(turn.context.messages)
            if message.tool_calls == (call,)
        )
        self.assertEqual(
            assistant_index + 2,
            _complete_round_tail(
                list(turn.context.messages), assistant_index, "native"
            ),
        )
        self.assertEqual(
            [
                RoundStarted,
                ToolCallCompleted,
                ProviderCompleted,
                ToolExecutionStarted,
                ToolExecutionCompleted,
                RuntimeCompleted,
            ],
            [type(event) for event in events],
        )

    def test_provider_failure_before_complete_round_records_closed_failure(self) -> None:
        provider = StructuredScriptedProvider([ProviderError("synthetic")])
        original = SessionContext()

        turn = CodingAgent(
            provider,
            self.registry,
            plan_enabled=False,
            max_rounds=1,
        ).run_with_context("contract", original)

        self.assertFalse(turn.result.ok)
        self.assertEqual("模型请求失败，运行已安全停止", turn.result.summary)
        self.assertEqual(
            ["task", "task_termination"],
            [message.kind for message in turn.context.messages],
        )
        self.assertGreater(turn.context.latest_completed_task_seq, 0)
        self.assertEqual([], self.registry.call_ids)

    def test_batch_failure_executes_only_root_and_pairs_skipped_remainder(self) -> None:
        calls = (
            ToolCall("bad", "create_file", {}),
            ToolCall("write", "create_file", {"path": "no.txt", "content": "no"}),
            ToolCall("finish", "finish", {"summary": "wrong"}),
        )
        provider = StructuredScriptedProvider(
            [ProviderResponse(tool_calls=calls, finish_reason="tool_calls")]
        )

        turn = CodingAgent(
            provider,
            self.registry,
            plan_enabled=False,
            max_rounds=1,
        ).run_with_context("contract", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual(["bad"], self.registry.call_ids)
        self.assertEqual(1, turn.result.tool_calls)
        self.assertFalse((self.workspace / "no.txt").exists())
        tool_results = [
            message for message in turn.context.messages if message.role == "tool"
        ]
        self.assertEqual([call.id for call in calls], [m.tool_call_id for m in tool_results])
        self.assertEqual(
            "skipped",
            json.loads(tool_results[1].content or "")
            ["tool_result"]["error"]["code"],
        )
        self.assertEqual(
            "skipped",
            json.loads(tool_results[2].content or "")
            ["tool_result"]["error"]["code"],
        )

    def test_history_compatibility_exports_share_one_implementation(self) -> None:
        from tricoder import agent
        from tricoder.context import history

        self.assertIs(agent.compact_messages, history.compact_messages)
        self.assertIs(agent.compact_session_messages, history.compact_session_messages)
        self.assertIs(agent._complete_round_tail, history.complete_round_tail)
        self.assertIs(agent._is_complete_tool_round, history.is_complete_tool_round)

    def test_observer_compatibility_exports_share_one_implementation(self) -> None:
        from tricoder import agent
        from tricoder.engine import telemetry

        self.assertIs(agent.AgentObserver, telemetry.AgentObserver)
        self.assertIs(agent.NullObserver, telemetry.NullObserver)
        self.assertIs(agent.ProviderUsageObserver, telemetry.ProviderUsageObserver)

    def test_provider_request_and_planning_helpers_have_stable_boundaries(self) -> None:
        from tricoder import agent
        from tricoder.engine import provider_request

        self.assertEqual(agent.PLANNING_PROMPT, provider_request.PLANNING_PROMPT)
        self.assertEqual(
            "1. inspect\n2. edit\n3. verify",
            provider_request.parse_plan(
                '{"steps": ["inspect", "edit", "verify"]}'
            ),
        )
        response = ProviderResponse(
            content="text",
            tool_calls=(ToolCall("call", "finish", {"summary": "done"}),),
            finish_reason="tool_calls",
        )
        self.assertEqual(
            ["TextDelta", "ToolCallCompleted", "ProviderCompleted"],
            [type(event).__name__ for event in provider_request.response_events(response)],
        )

    def test_run_state_is_task_local_and_keeps_raw_history_separate(self) -> None:
        from tricoder.context.memory import ConversationMemory, MemoryItem
        from tricoder.engine.state import AgentRunState

        context = SessionContext(conversation_memory=ConversationMemory())
        first = AgentRunState.start("system", "one", context)
        second = AgentRunState.start("system", "two", context)

        first.modified_files.append("first.py")
        self.assertEqual([], second.modified_files)
        self.assertEqual([], list(context.modified_files))
        raw_count = len(first.messages)
        first.conversation_memory = ConversationMemory(
            goal=MemoryItem("g", "remember", ("m1",), "session")
        )
        request = first.request_view(structured_memory=True)
        self.assertGreater(len(request), raw_count)
        self.assertEqual(raw_count, len(first.messages))

    def test_memory_coordinator_is_a_distinct_internal_service(self) -> None:
        from tricoder.context.coordinator import MemoryCoordinator

        self.assertTrue(callable(getattr(MemoryCoordinator, "build_review_candidate", None)))
        self.assertTrue(callable(getattr(MemoryCoordinator, "prepare_compaction", None)))

    def test_reusing_agent_does_not_share_task_counters_or_messages(self) -> None:
        responses = [
            ProviderResponse(
                tool_calls=(
                    ToolCall("finish-one", "finish", {"summary": "one"}),
                ),
                finish_reason="tool_calls",
            ),
            ProviderResponse(
                tool_calls=(
                    ToolCall("finish-two", "finish", {"summary": "two"}),
                ),
                finish_reason="tool_calls",
            ),
        ]
        agent = CodingAgent(
            StructuredScriptedProvider(responses),
            self.registry,
            plan_enabled=False,
            max_rounds=1,
        )

        first = agent.run_with_context("first", SessionContext())
        second = agent.run_with_context("second", SessionContext())

        self.assertEqual((1, 1), (first.result.tool_calls, second.result.tool_calls))
        self.assertFalse(any("first" in (m.content or "") for m in second.context.messages))
        self.assertFalse(any("second" in (m.content or "") for m in first.context.messages))


if __name__ == "__main__":
    unittest.main()
