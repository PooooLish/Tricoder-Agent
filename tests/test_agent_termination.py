"""ReAct 结束协议的累计纠错与安全停止回归测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.context.memory import ConversationMemory
from tricoder.context.summarizer import MemorySummaryResult
from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import ProviderCompleted
from tricoder.models import (
    MemoryConfig,
    ProviderResponse,
    SessionContext,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.protocols import NativeToolProtocol
from tricoder.tools import ToolContext, ToolRegistry


TERMINATION_FAILURE = (
    "结束协议纠正失败：本任务累计 3 次未提交工具调用，"
    "已停止继续请求；任务结果仍需确认。"
)
TERMINATION_NOTICE = (
    "本轮已停止：结束协议纠正失败；任务未完成，结果仍需确认。"
)


class StrictQueueProvider:
    """按顺序返回合成响应，并记录每一次业务请求。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.histories: list[list[object]] = []
        self.tool_batches: list[tuple[ToolDefinition, ...]] = []

    def complete(
        self,
        messages: list[object],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        self.tool_batches.append(tuple(tools))
        if not self.responses:
            raise AssertionError("Provider 响应队列已耗尽，Agent 发生了额外请求")
        return self.responses.pop(0)


class EmptyMemorySummarizer:
    """返回空语义候选，用于验证程序必须自行保留可信终止事实。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.calls.append(captured)
        covered = max(message.message_seq or 0 for message in captured)
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=covered,
            ),
            None,
        )


def finish_response(call_id: str = "finish-1") -> ProviderResponse:
    """构造标准原生 finish 调用。"""

    return ProviderResponse(
        tool_calls=(ToolCall(call_id, "finish", {"summary": "不应执行"}),),
        finish_reason="tool_calls",
    )


class NativeTerminationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approver=lambda _action, _detail: True,
                timeout=5,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_third_native_text_response_stops_before_fourth_request(self) -> None:
        """第三次原生无工具响应必须立即以未完成停止。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="任务完成"),
                ProviderResponse(content="确实已经完成"),
                ProviderResponse(content="最终答案"),
                finish_response("unexpected-fourth"),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=10,
            plan_enabled=False,
        ).run("检查结束协议")

        self.assertFalse(result.ok)
        self.assertEqual(TERMINATION_FAILURE, result.summary)
        self.assertEqual(3, result.rounds)
        self.assertEqual(3, len(provider.histories))
        self.assertEqual(0, result.tool_calls)

    def test_zero_tool_budget_stop_closes_history_without_advancing_success(self) -> None:
        """零工具失败任务必须可总结，但绝不能取得成功任务水位。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                ProviderResponse(content="文本三"),
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
        ).run_with_context("零工具失败任务", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual(0, turn.context.latest_completed_task_seq)
        terminal = turn.context.messages[-1]
        self.assertEqual("user", terminal.role)
        self.assertEqual("task_termination", terminal.kind)
        self.assertEqual(TERMINATION_NOTICE, terminal.content)
        self.assertEqual((), terminal.tool_calls)
        self.assertIsNone(terminal.tool_call_id)

        plan = ContextManager(
            ContextBudget(max_chars=100_000),
            NativeToolProtocol(),
        ).plan_save_candidate(turn.context.messages, covered_through=0)
        self.assertTrue(plan.needs_summary, plan.reason)
        self.assertEqual(terminal.message_seq, plan.covered_through)

    def test_failed_tool_task_can_compact_and_later_candidate_keeps_failure(self) -> None:
        """含工具的失败旧任务不得阻塞后续成功任务的压缩和保存候选。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall("list-before-stop", "list_files", {"path": "."}),
                    ),
                    finish_reason="tool_calls",
                ),
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                ProviderResponse(content="文本三"),
                finish_response("finish-next-task"),
            ]
        )
        summarizer = EmptyMemorySummarizer()
        agent = CodingAgent(
            provider,
            self.tools,
            max_rounds=10,
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        )

        failed = agent.run_with_context("先列出文件再结束", SessionContext())
        succeeded = agent.run_with_context("继续并正常结束", failed.context)

        self.assertFalse(failed.result.ok)
        self.assertEqual(0, failed.context.latest_completed_task_seq)
        self.assertTrue(succeeded.result.ok, succeeded.result.summary)
        self.assertEqual(1, len(summarizer.calls))
        self.assertTrue(
            any(message.kind == "task_termination" for message in summarizer.calls[0])
        )
        candidate = succeeded.context.review_memory_candidate
        self.assertIsNotNone(candidate)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(1, len(candidate.open_items))
        failure = candidate.open_items[0]
        self.assertEqual("pending", failure.state)
        self.assertIn("结束协议", failure.text)
        self.assertIn("未完成", failure.text)
        terminal = next(
            message
            for message in summarizer.calls[0]
            if message.kind == "task_termination"
        )
        self.assertEqual((f"m{terminal.message_seq}",), failure.source_ids)
        self.assertEqual(terminal.task_id, failure.task_id)

        history = succeeded.context.messages
        total_chars = sum(message.character_budget() for message in history)
        compaction = ContextManager(
            ContextBudget(max_chars=total_chars - 1),
            NativeToolProtocol(),
        ).plan_compaction(history, trigger_ratio=0.8, target_ratio=0.6)
        self.assertTrue(compaction.needs_compaction, compaction.reason)
        self.assertTrue(
            any(message.kind == "task_termination" for message in compaction.source_messages)
        )

    def test_first_two_native_text_responses_receive_counted_feedback(self) -> None:
        """前两次允许纠正，且反馈公开准确的累计预算。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="第一次文本"),
                ProviderResponse(content="第二次文本"),
                finish_response(),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=3,
            plan_enabled=False,
        ).run("检查纠错反馈")

        self.assertTrue(result.ok)
        first_feedback = provider.histories[1][-1]
        second_feedback = provider.histories[2][-1]
        self.assertIn("1/3", getattr(first_feedback, "content", "") or "")
        self.assertIn("2/3", getattr(second_feedback, "content", "") or "")
        self.assertIn("再次", getattr(second_feedback, "content", "") or "")
        self.assertIn("停止", getattr(second_feedback, "content", "") or "")

    def test_successful_tools_do_not_reset_native_text_budget(self) -> None:
        """只读工具成功不会清零同一任务的协议错误总预算。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(
                    tool_calls=(
                        ToolCall("read-1", "read_file", {"path": "sample.py"}),
                    ),
                    finish_reason="tool_calls",
                ),
                ProviderResponse(content="文本二"),
                ProviderResponse(
                    tool_calls=(
                        ToolCall("list-1", "list_files", {"path": "."}),
                    ),
                    finish_reason="tool_calls",
                ),
                ProviderResponse(content="文本三"),
                finish_response("unexpected-sixth"),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=10,
            plan_enabled=False,
        ).run("交替读取后结束")

        self.assertFalse(result.ok)
        self.assertEqual(TERMINATION_FAILURE, result.summary)
        self.assertEqual(5, len(provider.histories))
        self.assertEqual(2, result.tool_calls)

    def test_new_task_resets_native_text_budget_on_same_agent(self) -> None:
        """计数属于 AgentRunState，不得跨同一 Agent 的任务继承。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="任务一文本一"),
                ProviderResponse(content="任务一文本二"),
                finish_response("finish-task-1"),
                ProviderResponse(content="任务二文本一"),
                ProviderResponse(content="任务二文本二"),
                finish_response("finish-task-2"),
            ]
        )
        agent = CodingAgent(
            provider,
            self.tools,
            max_rounds=3,
            plan_enabled=False,
        )

        first = agent.run_with_context("任务一", SessionContext())
        second = agent.run_with_context("任务二", first.context)

        self.assertTrue(first.result.ok)
        self.assertTrue(second.result.ok)
        self.assertEqual(6, len(provider.histories))

    def test_two_agents_keep_native_text_budgets_isolated(self) -> None:
        """独立 Agent 即使共享工具注册表也不能共享纠错计数。"""

        providers = [
            StrictQueueProvider(
                [
                    ProviderResponse(content=f"{label}-一"),
                    ProviderResponse(content=f"{label}-二"),
                    finish_response(f"finish-{label}"),
                ]
            )
            for label in ("a", "b")
        ]

        results = [
            CodingAgent(
                provider,
                self.tools,
                max_rounds=3,
                plan_enabled=False,
            ).run(f"任务-{index}")
            for index, provider in enumerate(providers)
        ]

        self.assertTrue(all(result.ok for result in results))
        self.assertEqual([3, 3], [len(provider.histories) for provider in providers])

    def test_write_and_verification_survive_termination_budget_stop(self) -> None:
        """提前停止保留已提交文件与本地验证事实，不自动回滚。"""

        target = self.workspace / "created.py"
        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "create-1",
                            "create_file",
                            {"path": target.name, "content": "created = True\n"},
                        ),
                    )
                ),
                ProviderResponse(content="文本二"),
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "verify-1",
                            "run_command",
                            {"command": "python -m compileall -q created.py"},
                        ),
                    )
                ),
                ProviderResponse(content="文本三"),
                finish_response("unexpected-after-stop"),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=10,
            plan_enabled=False,
        ).run("创建并验证文件")

        self.assertFalse(result.ok)
        self.assertEqual(TERMINATION_FAILURE, result.summary)
        self.assertTrue(target.exists())
        self.assertEqual((target.name,), result.modified_files)
        self.assertEqual("通过", result.verification)
        self.assertEqual(2, result.tool_calls)
        self.assertEqual(5, len(provider.histories))

    def test_budget_stop_preserves_usage_and_audits_fixed_metadata(self) -> None:
        """第三轮用量与脱敏审计均必须在停止前提交。"""

        audit_path = self.workspace / "runtime" / "termination.jsonl"
        provider = StrictQueueProvider(
            [
                ProviderResponse(
                    content=f"PRIVATE-{index}",
                    usage=TokenUsage(index, index + 1, 0, index),
                )
                for index in range(1, 4)
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=10,
            plan_enabled=False,
            audit=AuditLogger(audit_path),
        ).run("审计结束预算")

        self.assertFalse(result.ok)
        self.assertEqual(TokenUsage(6, 9, 0, 6), result.usage)
        serialized = audit_path.read_text(encoding="utf-8")
        events = [json.loads(line) for line in serialized.splitlines()]
        invalid = [event for event in events if event.get("status") == "invalid_action"]
        self.assertEqual([1, 2, 3], [event["correction_count"] for event in invalid])
        self.assertEqual([False, False, True], [event["will_stop"] for event in invalid])
        self.assertTrue(
            all(event["reason"] == "native_missing_tool_call" for event in invalid)
        )
        self.assertTrue(all(event["correction_limit"] == 3 for event in invalid))
        self.assertNotIn("PRIVATE-", serialized)

    def test_non_missing_action_errors_do_not_consume_native_text_budget(self) -> None:
        """重复调用 ID 与普通工具失败不属于原生无工具响应预算。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall("duplicate", "read_file", {"path": "sample.py"}),
                        ToolCall("duplicate", "list_files", {"path": "."}),
                    )
                ),
                ProviderResponse(
                    tool_calls=(ToolCall("unknown", "missing_tool", {}),)
                ),
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                finish_response(),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
        ).run("其他错误不消耗预算")

        self.assertTrue(result.ok, result.summary)
        self.assertEqual(5, len(provider.histories))

    def test_legacy_parse_errors_do_not_use_native_termination_budget(self) -> None:
        """legacy JSON 纠错仍由 max_rounds 管理，不套用 native 专项预算。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="不是 JSON"),
                ProviderResponse(content="仍不是 JSON"),
                ProviderResponse(content="继续不是 JSON"),
                ProviderResponse(
                    content=json.dumps(
                        {
                            "tool": "finish",
                            "arguments": {"summary": "legacy 已纠正"},
                            "reason": "结束",
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=4,
            plan_enabled=False,
            tool_protocol="legacy_json",
        ).run("legacy 结束")

        self.assertTrue(result.ok, result.summary)
        self.assertEqual("legacy 已纠正", result.summary)

    def test_max_rounds_still_wins_before_third_missing_tool_response(self) -> None:
        """专项预算未耗尽时保留原有全局轮数兜底。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                finish_response("must-remain-unconsumed"),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=2,
            plan_enabled=False,
        ).run("轮数兜底")

        self.assertFalse(result.ok)
        self.assertEqual("达到最大轮数 2，任务已安全停止", result.summary)
        self.assertEqual(2, len(provider.histories))

    def test_cancellation_after_third_response_has_priority(self) -> None:
        """第三次响应到达时已取消，公开原因仍必须是取消。"""

        class Observer:
            def __init__(inner) -> None:
                inner.errors: list[str] = []

            def on_round_start(inner, _round: int, _maximum: int) -> None:
                return None

            def on_action(inner, _action: object) -> None:
                return None

            def on_tool_result(
                inner,
                _action: object,
                _result: object,
                _duration_ms: int,
            ) -> None:
                return None

            def on_error(inner, message: str) -> None:
                inner.errors.append(message)

        token = CancellationToken()
        observer = Observer()
        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                ProviderResponse(content="文本三"),
            ]
        )
        completions = 0

        def cancel_on_third(event: object) -> None:
            nonlocal completions
            if isinstance(event, ProviderCompleted):
                completions += 1
                if completions == 3:
                    token.cancel()

        turn = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
            observer=observer,
        ).run_with_context(
            "取消优先",
            SessionContext(),
            cancellation=token,
            event_sink=cancel_on_third,
        )

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(3, len(provider.histories))
        self.assertEqual("任务已取消", observer.errors[-1])
        self.assertNotIn(TERMINATION_FAILURE, observer.errors)
        self.assertFalse(
            any(message.kind == "task_termination" for message in turn.context.messages)
        )

    def test_terminal_audit_failure_does_not_publish_termination_marker(self) -> None:
        """专项停止审计失败时必须保留 fail-closed 路径，不提交终止标记。"""

        class FailingAudit:
            def prepare(self) -> None:
                return None

            def log(self, event: dict[str, object]) -> None:
                if (
                    event.get("status") == "invalid_action"
                    and event.get("correction_count") == 3
                ):
                    raise OSError("synthetic audit failure")

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                ProviderResponse(content="文本三"),
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
            audit=FailingAudit(),  # type: ignore[arg-type]
        ).run_with_context("审计失败", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("无法写入审计日志，运行已安全停止", turn.result.summary)
        self.assertFalse(
            any(message.kind == "task_termination" for message in turn.context.messages)
        )

    def test_cancellation_during_third_invalid_action_audit_has_priority(self) -> None:
        """审计期间到达的取消也必须覆盖随后准备返回的专项停止原因。"""

        token = CancellationToken()

        class CancellingAudit:
            def prepare(inner) -> None:
                return None

            def log(inner, event: dict[str, object]) -> None:
                if (
                    event.get("status") == "invalid_action"
                    and event.get("correction_count") == 3
                ):
                    token.cancel()

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                ProviderResponse(content="文本三"),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
            audit=CancellingAudit(),  # type: ignore[arg-type]
        ).run("审计期间取消", cancellation=token)

        self.assertTrue(token.is_cancelled)
        self.assertFalse(result.ok)
        self.assertEqual("任务已取消", result.summary)
        self.assertEqual(3, len(provider.histories))

    def test_length_or_empty_responses_never_end_task_by_themselves(self) -> None:
        """空响应和 length 文本只能纠错，不能绕过显式 finish。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(finish_reason="stop"),
                ProviderResponse(content="截断文本", finish_reason="length"),
                finish_response(),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=3,
            plan_enabled=False,
        ).run("显式结束")

        self.assertTrue(result.ok)
        self.assertEqual(3, len(provider.histories))

    def test_finish_stops_same_batch_and_keeps_all_tool_results_paired(self) -> None:
        """finish 后的写入只补 skipped，不能执行或形成孤立调用。"""

        target = self.workspace / "must-not-exist.py"
        provider = StrictQueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall("finish-first", "finish", {"summary": "结束"}),
                        ToolCall(
                            "write-after-finish",
                            "create_file",
                            {"path": target.name, "content": "bad = True\n"},
                        ),
                    )
                )
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            max_rounds=1,
            plan_enabled=False,
        ).run_with_context("同批结束", SessionContext())

        self.assertTrue(turn.result.ok)
        self.assertFalse(target.exists())
        self.assertEqual(1, turn.result.tool_calls)
        assistant = next(message for message in turn.context.messages if message.tool_calls)
        results = [message for message in turn.context.messages if message.role == "tool"]
        self.assertEqual(
            [call.id for call in assistant.tool_calls],
            [message.tool_call_id for message in results],
        )


    def test_finish_definition_exposes_required_summary_and_end_contract(self) -> None:
        """实际发送给 Provider 的 finish 定义必须说明结束语义。"""

        provider = StrictQueueProvider([finish_response()])

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=1,
            plan_enabled=False,
        ).run("结束")

        self.assertTrue(result.ok)
        definition = next(
            item for item in provider.tool_batches[0] if item.name == "finish"
        )
        self.assertIn("结束", definition.description)
        self.assertIn("本地验证", definition.description)
        self.assertEqual(["summary"], definition.parameters["required"])

    def test_plain_text_feedback_then_finish_executes_real_finish_call(self) -> None:
        """普通文本只能得到纠错；后续原生 finish 才能结束任务。"""

        provider = StrictQueueProvider(
            [
                ProviderResponse(content="已经完成", finish_reason="stop"),
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "finish-after-feedback",
                            "finish",
                            {"summary": "按结束协议完成"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=2,
            plan_enabled=False,
        ).run("按协议结束")

        self.assertTrue(result.ok)
        self.assertEqual("按结束协议完成", result.summary)
        self.assertEqual(2, len(provider.histories))
        feedback = provider.histories[1][-1]
        self.assertEqual("protocol_feedback", getattr(feedback, "kind", None))
        self.assertIn("finish", getattr(feedback, "content", "") or "")


class AsyncTerminationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_entrypoint_stops_after_third_native_text_response(self) -> None:
        """规范异步入口与同步包装共享同一累计停止语义。"""

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            tools = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(workspace),
                    CommandPolicy(),
                    approver=lambda _action, _detail: True,
                )
            )
            provider = StrictQueueProvider(
                [
                    ProviderResponse(content="文本一"),
                    ProviderResponse(content="文本二"),
                    ProviderResponse(content="文本三"),
                    finish_response("unexpected-fourth"),
                ]
            )
            agent = CodingAgent(
                provider,
                tools,
                max_rounds=10,
                plan_enabled=False,
            )

            turn = await asyncio.wait_for(
                agent.run_with_context_async("异步结束", SessionContext()),
                timeout=2,
            )

        self.assertFalse(turn.result.ok)
        self.assertEqual(TERMINATION_FAILURE, turn.result.summary)
        self.assertEqual(3, len(provider.histories))


if __name__ == "__main__":
    unittest.main()
