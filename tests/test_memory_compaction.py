from __future__ import annotations

import unittest

from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.context.memory import ConversationMemory, MemoryItem, MemoryValidationError
from tricoder.models import Message, SessionContext, ToolCall, ToolDefinition
from tricoder.protocols import NativeToolProtocol


def task_block(start: int, label: str, *, payload: int = 20) -> tuple[Message, ...]:
    call = ToolCall(f"call-{label}", "read_file", {"path": f"{label}.txt"})
    task_id = f"task-{start}"
    return (
        Message("user", f"任务 {label}", kind="task", message_seq=start, task_id=task_id),
        Message(
            "assistant",
            None,
            kind="tool_call",
            tool_calls=(call,),
            message_seq=start + 1,
            task_id=task_id,
        ),
        Message(
            "tool",
            "x" * payload,
            kind="tool_result",
            tool_call_id=call.id,
            message_seq=start + 2,
            task_id=task_id,
        ),
    )


class MemoryCompactionTests(unittest.TestCase):
    def test_plan_uses_complete_old_task_prefix_and_keeps_recent_history(self) -> None:
        messages = (*task_block(1, "a"), *task_block(4, "b"), *task_block(7, "c"), *task_block(10, "d"))
        total = sum(message.character_budget() for message in messages)
        manager = ContextManager(ContextBudget(max_chars=total - 1), NativeToolProtocol())

        plan = manager.plan_compaction(messages, trigger_ratio=0.80, target_ratio=0.60)

        self.assertTrue(plan.needs_compaction)
        self.assertEqual(tuple(messages[: len(plan.source_messages)]), plan.source_messages)
        self.assertEqual(0, len(plan.source_messages) % 3)
        self.assertEqual(plan.covered_through, plan.source_messages[-1].message_seq)
        self.assertEqual(tuple(messages[len(plan.source_messages) :]), plan.retained_messages)
        # 近期至少保留当前任务，工具调用与结果从不被拆开。
        self.assertGreaterEqual(len(plan.retained_messages), 3)
        for index in range(0, len(plan.retained_messages), 3):
            group = plan.retained_messages[index : index + 3]
            self.assertEqual(group[1].tool_calls[0].id, group[2].tool_call_id)

    def test_plan_accepts_a_closed_task_with_protocol_correction_noise(self) -> None:
        """纠错对之后已有完整工具回合时，旧任务仍应成为原子摘要来源。"""

        corrected_call = ToolCall("call-fixed", "read_file", {"path": "fixed.txt"})
        noisy = (
            Message("user", "任务 noisy", kind="task", message_seq=1, task_id="task-1"),
            Message("assistant", "先错误返回文本", message_seq=2, task_id="task-1"),
            Message(
                "user",
                "请改用工具调用",
                kind="protocol_feedback",
                message_seq=3,
                task_id="task-1",
            ),
            Message(
                "assistant",
                None,
                kind="tool_call",
                tool_calls=(corrected_call,),
                message_seq=4,
                task_id="task-1",
            ),
            Message(
                "tool",
                "x" * 120,
                kind="tool_result",
                tool_call_id=corrected_call.id,
                message_seq=5,
                task_id="task-1",
            ),
        )
        latest = task_block(6, "latest", payload=120)
        messages = (*noisy, *latest)
        manager = ContextManager(ContextBudget(max_chars=220), NativeToolProtocol())

        plan = manager.plan_compaction(
            messages,
            trigger_ratio=0.8,
            target_ratio=0.6,
        )

        self.assertTrue(plan.needs_compaction)
        self.assertEqual(noisy, plan.source_messages)
        self.assertEqual(latest, plan.retained_messages)

    def test_commit_is_atomic_shortens_context_and_rejects_old_generation(self) -> None:
        messages = (*task_block(1, "a"), *task_block(4, "b"), *task_block(7, "c"))
        manager = ContextManager(ContextBudget(max_chars=220), NativeToolProtocol())
        context = SessionContext(messages=messages, next_message_seq=10)
        plan = manager.plan_compaction(messages, trigger_ratio=0.8, target_ratio=0.6)
        source_id = f"m{plan.source_messages[0].message_seq}"
        candidate = ConversationMemory(
            generation=0,
            covered_through=plan.covered_through,
            constraints=(MemoryItem("keep-api", "不改公共接口", (source_id,), "session"),),
        )

        committed = manager.commit_compaction(context, plan, candidate)

        self.assertLess(len(committed.messages), len(context.messages))
        self.assertEqual(plan.retained_messages, committed.messages)
        self.assertEqual(1, committed.conversation_memory.revision)
        self.assertEqual(10, committed.next_message_seq)
        self.assertEqual(committed, manager.commit_compaction(committed, plan, candidate))

        stale = ConversationMemory(
            revision=committed.conversation_memory.revision,
            generation=-1,
            covered_through=plan.covered_through,
        )
        with self.assertRaises(MemoryValidationError):
            manager.commit_compaction(context, plan, stale)
        self.assertEqual(messages, context.messages)

    def test_tool_schema_and_output_reserve_participate_in_full_budget(self) -> None:
        messages = (*task_block(1, "a"), *task_block(4, "b"), *task_block(7, "c"))
        manager = ContextManager(ContextBudget(max_chars=900), NativeToolProtocol())
        huge_tool = ToolDefinition(
            "large_schema",
            "d" * 400,
            {"type": "object", "properties": {"value": {"description": "s" * 400}}},
        )

        without_fixed = manager.plan_compaction(messages, trigger_ratio=0.8, target_ratio=0.6)
        with_fixed = manager.plan_compaction(
            messages,
            tools=(huge_tool,),
            output_reserve_chars=100,
            trigger_ratio=0.8,
            target_ratio=0.6,
        )

        self.assertFalse(without_fixed.needs_compaction)
        self.assertTrue(with_fixed.needs_compaction)
        self.assertGreater(with_fixed.before_chars, without_fixed.before_chars)

    def test_oversized_fixed_or_latest_group_stops_instead_of_looping(self) -> None:
        manager = ContextManager(ContextBudget(max_chars=100), NativeToolProtocol())
        fixed_only = manager.plan_compaction(
            (Message("system", "x" * 200),),
            trigger_ratio=0.8,
            target_ratio=0.6,
        )
        latest_only = manager.plan_compaction(
            task_block(1, "huge", payload=400),
            trigger_ratio=0.8,
            target_ratio=0.6,
        )

        for plan in (fixed_only, latest_only):
            self.assertFalse(plan.needs_compaction)
            self.assertTrue(plan.over_hard_limit)
            self.assertEqual((), plan.source_messages)
            self.assertTrue(plan.reason)

    def test_token_mode_recomputes_after_prefix_replacement(self) -> None:
        messages = (*task_block(1, "a", payload=100), *task_block(4, "b", payload=100), *task_block(7, "c", payload=100))
        manager = ContextManager(ContextBudget(max_tokens=160), NativeToolProtocol())

        plan = manager.plan_compaction(
            messages,
            output_reserve_tokens=20,
            trigger_ratio=0.8,
            target_ratio=0.5,
        )

        self.assertTrue(plan.needs_compaction)
        self.assertLess(plan.after_estimated_tokens, plan.before_estimated_tokens)


if __name__ == "__main__":
    unittest.main()
