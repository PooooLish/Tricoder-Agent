import sys
import unittest
from pathlib import Path


# 让 ``python -m unittest`` 在未安装包的源码工作树中也能直接发现 ``src``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.models import Message, ProviderResponse, ToolCall, ToolDefinition


class StructuredModelTests(unittest.TestCase):
    def test_tool_definition_rejects_blank_name_and_non_object_parameters(self) -> None:
        """防止没有可调用名称或参数对象的工具定义进入 Provider 适配层。"""
        with self.assertRaises(ValueError):
            ToolDefinition(name="", description="读取文件", parameters={})
        with self.assertRaises(ValueError):
            ToolDefinition(
                name="read_file",
                description="读取文件",
                parameters=["path"],  # type: ignore[arg-type]
            )

    def test_tool_call_keeps_provider_call_identity_name_and_object_arguments(self) -> None:
        """防止工具调用丢失 Provider 返回的关联 ID 或结构化参数。"""
        call = ToolCall(id="call_123", name="read_file", arguments={"path": "main.py"})

        self.assertEqual("call_123", call.id)
        self.assertEqual("read_file", call.name)
        self.assertEqual({"path": "main.py"}, call.arguments)

    def test_provider_response_combines_text_tool_calls_and_finish_reason(self) -> None:
        """防止 Provider 响应在文本、多个调用或结束原因之间互相排斥。"""
        calls = (
            ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"}),
            ToolCall(id="call_2", name="list_files", arguments={"path": "src"}),
        )
        response = ProviderResponse(
            content="我会先检查文件。",
            tool_calls=calls,
            finish_reason="tool_calls",
        )

        self.assertEqual("我会先检查文件。", response.content)
        self.assertEqual(calls, response.tool_calls)
        self.assertEqual("tool_calls", response.finish_reason)

    def test_message_represents_text_assistant_calls_and_tool_result(self) -> None:
        """防止会话模型无法表达 Provider 无关的三种消息形态。"""
        call = ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})

        text = Message(role="user", content="请读取文件")
        assistant_calls = Message(role="assistant", content=None, tool_calls=(call,))
        tool_result = Message(role="tool", content="文件内容", tool_call_id="call_1")

        self.assertEqual("请读取文件", text.content)
        self.assertEqual((call,), assistant_calls.tool_calls)
        self.assertEqual("call_1", tool_result.tool_call_id)

    def test_message_rejects_invalid_tool_message_combinations(self) -> None:
        """防止缺少关联 ID 的工具结果或普通消息伪装成工具结果。"""
        with self.assertRaises(ValueError):
            Message(role="tool", content="文件内容")
        with self.assertRaises(ValueError):
            Message(role="user", content="普通消息", tool_call_id="call_1")
        with self.assertRaises(ValueError):
            Message(
                role="user",
                content="普通消息",
                tool_calls=(ToolCall(id="call_1", name="read_file", arguments={}),),
            )

    def test_message_character_budget_includes_structured_tool_call_data(self) -> None:
        """防止上下文压缩只计算文本而遗漏工具名、调用 ID 与参数。"""
        call = ToolCall(
            id="call_123",
            name="read_file",
            arguments={"path": "src/main.py"},
        )
        message = Message(role="assistant", content="准备读取", tool_calls=(call,))

        self.assertEqual(
            len("assistant准备读取call_123read_file{'path': 'src/main.py'}"),
            message.character_budget(),
        )


if __name__ == "__main__":
    unittest.main()
