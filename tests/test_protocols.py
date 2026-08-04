"""协议对象与 Agent 完整回合判定的回归测试。"""

import unittest

from tricoder.agent import _is_complete_tool_round
from tricoder.models import Message, ToolCall
from tricoder.protocols import LegacyJsonProtocol, NativeToolProtocol


def _tool_result(role: str, *, kind: str = "tool_result") -> Message:
    return Message(role, "output", kind=kind)


class ProtocolCompleteRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native = NativeToolProtocol()
        self.legacy = LegacyJsonProtocol()

    def test_native_complete_round_requires_exact_single_call_match(self) -> None:
        """原生协议的完整回合必须恰好一个调用且 id 与工具结果一致。"""
        assistant = Message("assistant", None, tool_calls=(ToolCall("a", "read_file", {}),))
        self.assertTrue(
            self.native.complete_round(
                assistant, Message("tool", "ok", kind="tool_result", tool_call_id="a")
            )
        )
        mismatched_id = Message(
            "tool", "ok", kind="tool_result", tool_call_id="b"
        )
        self.assertFalse(self.native.complete_round(assistant, mismatched_id))
        wrong_kind = Message("tool", "ok", kind="generic", tool_call_id="a")
        self.assertFalse(self.native.complete_round(assistant, wrong_kind))
        wrong_role = Message("user", "ok", kind="tool_result")
        self.assertFalse(self.native.complete_round(assistant, wrong_role))
        multiple = Message(
            "assistant",
            None,
            tool_calls=(
                ToolCall("a", "read_file", {}),
                ToolCall("b", "search_text", {}),
            ),
        )
        self.assertFalse(
            self.native.complete_round(
                multiple, Message("tool", "ok", kind="tool_result", tool_call_id="a")
            )
        )

    def test_legacy_complete_round_strict_checks_kind(self) -> None:
        """legacy 严格判定要求 user 结果带 tool_result 标记。"""
        assistant = Message("assistant", '{"tool":"read_file","arguments":{},"reason":"x"}')
        self.assertTrue(self.legacy.complete_round(assistant, _tool_result("user")))
        wrong_kind = _tool_result("user", kind="generic")
        self.assertFalse(self.legacy.complete_round(assistant, wrong_kind))
        self.assertTrue(self.legacy.complete_round_loose(assistant, wrong_kind))

    def test_legacy_loose_matches_untagged_user_result(self) -> None:
        """宽松判定不检查 kind，供历史压缩对旧消息兼容。"""
        assistant = Message("assistant", "plain")
        loose_result = _tool_result("user", kind="generic")
        self.assertTrue(self.legacy.complete_round_loose(assistant, loose_result))
        self.assertFalse(self.legacy.complete_round(assistant, loose_result))

    def test_legacy_loose_rejects_assistant_with_tool_calls(self) -> None:
        """legacy 宽松判定仍要求 assistant 不带原生工具调用。"""
        assistant = Message("assistant", None, tool_calls=(ToolCall("a", "read_file", {}),))
        self.assertFalse(self.legacy.complete_round_loose(assistant, _tool_result("user")))

    def test_is_complete_tool_round_none_uses_loose_any(self) -> None:
        """None 协议用宽松判定匹配任一协议，兼容旧 user 消息。"""
        assistant = Message("assistant", '{"tool":"finish","arguments":{},"reason":"x"}')
        legacy_untagged = _tool_result("user", kind="generic")
        self.assertTrue(_is_complete_tool_round(assistant, legacy_untagged))

    def test_is_complete_tool_round_explicit_uses_strict(self) -> None:
        """显式协议使用严格判定，legacy 未标记的 user 结果不被接受。"""
        assistant = Message("assistant", '{"tool":"finish","arguments":{},"reason":"x"}')
        legacy_untagged = _tool_result("user", kind="generic")
        self.assertFalse(
            _is_complete_tool_round(assistant, legacy_untagged, "legacy_json")
        )

    def test_is_complete_tool_round_rejects_non_assistant(self) -> None:
        """首条消息不是 assistant 时任何协议都不构成完整回合。"""
        user_message = Message("user", "task")
        tool_result = Message("tool", "ok", kind="tool_result", tool_call_id="a")
        self.assertFalse(_is_complete_tool_round(user_message, tool_result))


if __name__ == "__main__":
    unittest.main()
