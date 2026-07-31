import unittest
from unittest.mock import patch

from tricoder.models import (
    Message,
    ProviderConfig,
    ProviderResponse,
    ToolCall,
    ToolDefinition,
)
from tricoder.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    ProviderProtocolError,
    UrllibTransport,
)


class FakeHttpResponse:
    """仅替代真实网络响应流，保留 Transport 的完整解码路径。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class RecordingTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


def make_provider(
    provider_name: str,
    response: dict[str, object],
) -> tuple[OpenAICompatibleProvider, RecordingTransport]:
    transport = RecordingTransport([response])
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            name=provider_name,
            api_key="test-secret",
            base_url=f"https://{provider_name}.example.test/v1/",
            model=f"{provider_name}-model",
        ),
        transport=transport,
        timeout=9,
        sleeper=lambda _: None,
    )
    return provider, transport


WEATHER_TOOL = ToolDefinition(
    name="weather",
    description="查询天气",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)


class ProviderTests(unittest.TestCase):
    def test_urllib_transport_rejects_invalid_json_as_protocol_error(self) -> None:
        """防止已收到的无效 JSON 被误分类为普通 Provider 故障。"""
        transport = UrllibTransport()

        with patch(
            "tricoder.providers.urllib.request.urlopen",
            return_value=FakeHttpResponse(b"not-json"),
        ):
            with self.assertRaises(ProviderError) as caught:
                transport.post_json(
                    "https://example.test/chat/completions",
                    {"Authorization": "Bearer test-key"},
                    {"model": "test-model", "messages": []},
                    3,
                )

        self.assertIsInstance(caught.exception, ProviderProtocolError)
        self.assertFalse(caught.exception.retryable)

    def test_urllib_transport_rejects_non_object_json_as_protocol_error(self) -> None:
        """防止顶层数组绕过统一厂商响应对象契约。"""
        transport = UrllibTransport()

        with patch(
            "tricoder.providers.urllib.request.urlopen",
            return_value=FakeHttpResponse(b"[]"),
        ):
            with self.assertRaises(ProviderError) as caught:
                transport.post_json(
                    "https://example.test/chat/completions",
                    {"Authorization": "Bearer test-key"},
                    {"model": "test-model", "messages": []},
                    3,
                )

        self.assertIsInstance(caught.exception, ProviderProtocolError)
        self.assertFalse(caught.exception.retryable)

    def test_returns_unified_text_response_and_preserves_finish_reason(self) -> None:
        """防止普通文本响应绕过统一响应契约。"""
        provider, _ = make_provider(
            "deepseek",
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "已完成"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

        response = provider.complete([Message(role="user", content="修复测试")], [])

        self.assertEqual(
            ProviderResponse(content="已完成", finish_reason="stop"),
            response,
        )

    def test_serializes_assistant_tool_calls_and_tool_results(self) -> None:
        """防止多轮原生工具消息退化成普通文本或丢失调用关联 ID。"""
        provider, transport = make_provider(
            "openai",
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "继续"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
        messages = [
            Message(role="user", content="读取说明"),
            Message(
                role="assistant",
                content=None,
                tool_calls=(
                    ToolCall(
                        id="call-7",
                        name="read_file",
                        arguments={"path": "README.md"},
                    ),
                ),
            ),
            Message(role="tool", content="项目说明", tool_call_id="call-7"),
        ]

        provider.complete(messages, [])

        self.assertEqual(
            [
                {"role": "user", "content": "读取说明"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-7",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "README.md"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "项目说明",
                    "tool_call_id": "call-7",
                },
            ],
            transport.calls[0]["payload"]["messages"],  # type: ignore[index]
        )

    def test_parses_native_tool_calls_into_unified_response(self) -> None:
        """防止厂商 JSON 字符串参数泄漏到 Agent 或被当成普通文本。"""
        provider, _ = make_provider(
            "glm",
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-weather",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": '{"city":"北京"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

        response = provider.complete([Message("user", "天气如何")], [WEATHER_TOOL])

        self.assertEqual(
            ProviderResponse(
                tool_calls=(
                    ToolCall(
                        id="call-weather",
                        name="weather",
                        arguments={"city": "北京"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            response,
        )

    def test_rejects_malformed_native_tool_calls(self) -> None:
        """防止不完整或无法解析的工具调用进入执行层。"""
        malformed_calls = {
            "无效 arguments": {
                "id": "call-1",
                "type": "function",
                "function": {"name": "weather", "arguments": "{"},
            },
            "非对象 arguments": {
                "id": "call-1",
                "type": "function",
                "function": {"name": "weather", "arguments": "[]"},
            },
            "错误调用类型": {
                "id": "call-1",
                "type": "custom",
                "function": {"name": "weather", "arguments": "{}"},
            },
            "缺少 ID": {
                "type": "function",
                "function": {"name": "weather", "arguments": "{}"},
            },
            "缺少工具名": {
                "id": "call-1",
                "type": "function",
                "function": {"arguments": "{}"},
            },
        }
        for label, malformed_call in malformed_calls.items():
            with self.subTest(label=label):
                provider, _ = make_provider(
                    "openai",
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "tool_calls": [malformed_call],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                )

                with self.assertRaises(ProviderProtocolError):
                    provider.complete([Message("user", "hello")], [WEATHER_TOOL])

    def test_rejects_malformed_response_structure(self) -> None:
        """防止上游错误对象变成难以定位的下标或类型错误。"""
        for malformed_response in (
            {"choices": []},
            {"error": {"message": "bad request"}},
            {"choices": [{"message": {"content": 7}}]},
        ):
            with self.subTest(response=malformed_response):
                provider, _ = make_provider("glm", malformed_response)

                with self.assertRaisesRegex(ProviderProtocolError, "响应格式"):
                    provider.complete([Message("user", "hello")], [])

    def test_provider_profiles_emit_only_supported_tool_fields(self) -> None:
        """防止三家协议差异上浮到 Agent，或向厂商发送未声明支持的字段。"""
        expected_extras = {
            "openai": {
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
            "deepseek": {"tool_choice": "auto"},
            "glm": {"tool_choice": "auto"},
        }
        for provider_name, extras in expected_extras.items():
            with self.subTest(provider=provider_name):
                provider, transport = make_provider(
                    provider_name,
                    {
                        "choices": [
                            {
                                "message": {"content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )

                provider.complete([Message("user", "hello")], [WEATHER_TOOL])

                expected_function: dict[str, object] = {
                    "name": "weather",
                    "description": "查询天气",
                    "parameters": WEATHER_TOOL.parameters,
                }
                if provider_name == "openai":
                    expected_function["strict"] = True
                payload = transport.calls[0]["payload"]
                self.assertEqual(
                    [{"type": "function", "function": expected_function}],
                    payload["tools"],  # type: ignore[index]
                )
                for field, value in extras.items():
                    self.assertEqual(value, payload[field])  # type: ignore[index]
                self.assertEqual(
                    provider_name == "openai",
                    "parallel_tool_calls" in payload,
                )

    def test_omits_all_tool_fields_when_no_tools_are_available(self) -> None:
        """防止旧版纯文本协议收到不兼容的空工具控制字段。"""
        provider, transport = make_provider(
            "openai",
            {
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )

        provider.complete([Message("user", "hello")], [])

        payload = transport.calls[0]["payload"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("parallel_tool_calls", payload)

    def test_builds_openai_compatible_request(self) -> None:
        """防止地址、鉴权头或公共请求结构发生回归。"""
        provider, transport = make_provider(
            "deepseek",
            {
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

        provider.complete([Message(role="user", content="修复测试")], [])

        self.assertEqual(
            "https://deepseek.example.test/v1/chat/completions",
            transport.calls[0]["url"],
        )
        self.assertEqual(
            "Bearer test-secret",
            transport.calls[0]["headers"]["Authorization"],  # type: ignore[index]
        )
        self.assertEqual(
            {
                "model": "deepseek-model",
                "messages": [{"role": "user", "content": "修复测试"}],
            },
            transport.calls[0]["payload"],
        )

    def test_retries_retryable_errors(self) -> None:
        """防止短暂限流导致 Agent 立即丢失整个任务。"""
        transport = RecordingTransport(
            [
                ProviderError("限流", retryable=True),
                ProviderError("服务错误", retryable=True),
                {
                    "choices": [
                        {
                            "message": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            ]
        )
        delays: list[float] = []
        provider = OpenAICompatibleProvider(
            ProviderConfig("openai", "test-key", "https://api.openai.com/v1", "gpt-5"),
            transport=transport,
            timeout=10,
            sleeper=delays.append,
        )

        self.assertEqual(
            ProviderResponse(content="ok", finish_reason="stop"),
            provider.complete([Message("user", "hello")], []),
        )
        self.assertEqual(3, len(transport.calls))
        self.assertEqual([1.0, 2.0], delays)


if __name__ == "__main__":
    unittest.main()
