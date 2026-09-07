"""ContextManager 的 token/字符预算与完整工具回合契约。"""

from __future__ import annotations

import unittest

from tricoder.context import ContextBudget, ContextManager
from tricoder.models import Message, TokenUsage, ToolCall
from tricoder.protocols import _PROTOCOLS


class ContextManagerTests(unittest.TestCase):
    def test_token_budget_keeps_fixed_system_and_latest_task_when_they_exceed_limit(self) -> None:
        """预算再小也不能删掉决定行为的 system 与当前 task。"""
        messages = [
            Message("system", "不可删除的规则"),
            Message("user", "当前任务", kind="task"),
        ]
        manager = ContextManager(ContextBudget(max_tokens=1), _PROTOCOLS["native"])

        snapshot = manager.prepare(messages)

        self.assertEqual(tuple(messages), snapshot.messages)
        self.assertGreater(snapshot.estimated_tokens, 1)
        self.assertFalse(snapshot.compacted)

    def test_token_budget_keeps_a_complete_multi_call_round(self) -> None:
        """删除任一 tool result 都会破坏 Provider 的原生调用配对。"""
        calls = (
            ToolCall("call-a", "read_file", {"path": "a.py"}),
            ToolCall("call-b", "read_file", {"path": "b.py"}),
        )
        messages = [
            Message("system", "规则"),
            Message("user", "任务", kind="task"),
            Message("assistant", None, tool_calls=calls),
            Message("tool", "A", kind="tool_result", tool_call_id="call-a"),
            Message("tool", "B", kind="tool_result", tool_call_id="call-b"),
        ]
        manager = ContextManager(ContextBudget(max_tokens=10_000), _PROTOCOLS["native"])

        snapshot = manager.prepare(messages)

        self.assertEqual(tuple(messages), snapshot.messages)
        self.assertFalse(snapshot.compacted)

    def test_token_budget_drops_orphan_call_and_result_even_when_budget_fits(self) -> None:
        """孤立调用或结果不能被发送成无归属的协议历史。"""
        messages = [
            Message("system", "规则"),
            Message("user", "任务", kind="task"),
            Message(
                "assistant",
                None,
                tool_calls=(ToolCall("call-a", "read_file", {"path": "a.py"}),),
            ),
            Message("tool", "错误 ID", kind="tool_result", tool_call_id="call-b"),
        ]
        manager = ContextManager(ContextBudget(max_tokens=10_000), _PROTOCOLS["native"])

        snapshot = manager.prepare(messages)

        self.assertEqual(["system", "user"], [m.role for m in snapshot.messages])
        self.assertEqual(2, len(snapshot.messages))
        self.assertTrue(snapshot.compacted)

    def test_token_budget_keeps_an_oversized_latest_round_as_a_unit(self) -> None:
        """当前最后一轮即使超限也必须成对保留，供 Provider 延续协议。"""
        messages = [
            Message("system", "规则"),
            Message("user", "任务", kind="task"),
            Message(
                "assistant",
                None,
                tool_calls=(ToolCall("call-large", "read_file", {"path": "a.py"}),),
            ),
            Message(
                "tool",
                "x" * 2_000,
                kind="tool_result",
                tool_call_id="call-large",
            ),
        ]
        manager = ContextManager(ContextBudget(max_tokens=30), _PROTOCOLS["native"])

        snapshot = manager.prepare(messages)

        self.assertEqual("call-large", snapshot.messages[-1].tool_call_id)
        self.assertEqual(("call-large",), tuple(call.id for call in snapshot.messages[-2].tool_calls))
        self.assertGreater(snapshot.estimated_tokens, 30)
        self.assertTrue(snapshot.compacted)

    def test_record_usage_anchors_the_exact_prepared_prefix(self) -> None:
        """若历史前缀未变化，应优先使用 Provider 的真实 input/output 用量。"""
        messages = [Message("system", "规则"), Message("user", "任务", kind="task")]
        manager = ContextManager(ContextBudget(max_tokens=10_000), _PROTOCOLS["native"])
        manager.record_usage(TokenUsage(input_tokens=10, output_tokens=2), messages)

        snapshot = manager.prepare(messages)

        self.assertEqual(12, snapshot.estimated_tokens)
        self.assertEqual("provider", snapshot.token_source)

    def test_missing_input_usage_uses_a_conservative_local_estimate(self) -> None:
        """只有 output 用量时不能把未知 prompt token 当成零。"""
        messages = [Message("system", "规则"), Message("user", "任务", kind="task")]
        manager = ContextManager(ContextBudget(max_tokens=10_000), _PROTOCOLS["native"])
        manager.record_usage(TokenUsage(output_tokens=1), messages)

        snapshot = manager.prepare(messages)

        self.assertGreater(snapshot.estimated_tokens, 1)
        self.assertEqual("estimate", snapshot.token_source)

    def test_character_mode_matches_legacy_session_compaction_order(self) -> None:
        """迁移后旧字符预算样本的保留顺序必须保持不变。"""
        old = [
            Message("user", "旧任务", kind="task"),
            Message("assistant", "旧动作"),
            Message("user", "旧结果", kind="tool_result"),
        ]
        latest = [
            Message("user", "当前任务", kind="task"),
            Message("assistant", "当前动作"),
            Message("user", "当前结果", kind="tool_result"),
        ]
        messages = [Message("system", "规则"), *old, *latest]
        expected = [Message("system", "规则"), *old, *latest]
        max_chars = sum(message.character_budget() for message in expected)
        manager = ContextManager(ContextBudget(max_chars=max_chars), _PROTOCOLS["legacy_json"])

        snapshot = manager.prepare(messages)

        self.assertEqual(tuple(expected), snapshot.messages)

    def test_character_mode_drops_an_incomplete_old_task_block(self) -> None:
        """字符兼容模式继续剔除旧任务中的孤立动作。"""
        messages = [
            Message("system", "规则"),
            Message("user", "旧任务", kind="task"),
            Message("assistant", "孤立动作"),
            Message("user", "当前任务", kind="task"),
            Message("assistant", "当前动作"),
            Message("user", "当前结果", kind="tool_result"),
        ]
        manager = ContextManager(ContextBudget(max_chars=10_000), _PROTOCOLS["legacy_json"])

        snapshot = manager.prepare(messages)

        self.assertNotIn("旧任务", [message.content for message in snapshot.messages])
        self.assertEqual("当前任务", snapshot.messages[-3].content)


if __name__ == "__main__":
    unittest.main()
