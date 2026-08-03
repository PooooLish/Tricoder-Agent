import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.models import (
    Message,
    ProviderConfig,
    ProviderResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    ProviderProtocolError,
    UrllibTransport,
    _stable_json_bytes,
    create_provider,
)
from tricoder.tools import ToolContext, ToolRegistry


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


class ReadFailingHttpResponse:
    """在真实 response.read 边界模拟连接中断。"""

    def __init__(self, message: str) -> None:
        self._message = message

    def __enter__(self) -> "ReadFailingHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        raise ConnectionResetError(self._message)


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
    def test_normalizes_usage_for_each_provider_dialect(self) -> None:
        cases = {
            "openai": (
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 75},
                },
                TokenUsage(100, 20, 75, None),
            ),
            "deepseek": (
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 70,
                    "prompt_cache_miss_tokens": 30,
                },
                TokenUsage(100, 20, 70, 30),
            ),
            "glm": (
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 65},
                },
                TokenUsage(100, 20, 65, None),
            ),
        }

        for provider_name, (usage, expected) in cases.items():
            with self.subTest(provider=provider_name):
                provider, _ = make_provider(
                    provider_name,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": usage,
                    },
                )

                response = provider.complete([Message("user", "hello")], [])

                self.assertEqual(expected, response.usage)

    def test_ignores_malformed_optional_usage_without_rejecting_content(self) -> None:
        malformed_usages = (
            {"prompt_tokens": True},
            {"completion_tokens": "20"},
            {"prompt_tokens_details": {"cached_tokens": -1}},
        )

        for usage in malformed_usages:
            with self.subTest(usage=usage):
                provider, _ = make_provider(
                    "openai",
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": usage,
                    },
                )

                response = provider.complete([Message("user", "hello")], [])

                self.assertEqual("ok", response.content)
                self.assertIsNone(response.usage)

    def test_preserves_valid_partial_usage_fields(self) -> None:
        provider, _ = make_provider(
            "deepseek",
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_cache_miss_tokens": 30,
                    "prompt_cache_hit_tokens": "70",
                },
            },
        )

        response = provider.complete([Message("user", "hello")], [])

        self.assertEqual(TokenUsage(100, None, None, 30), response.usage)

    def test_factory_selects_each_registered_provider_profile(self) -> None:
        """防止共享 Factory 忽略厂商名称并退回无能力的通用档案。"""
        expected_capabilities = {
            "openai": (True, False, True, True, True),
            "deepseek": (True, False, False, True, True),
            "glm": (True, False, False, False, True),
        }

        for provider_name, expected in expected_capabilities.items():
            with self.subTest(provider=provider_name):
                provider = create_provider(
                    ProviderConfig(
                        provider_name,
                        "test-key",
                        "https://example.test/v1",
                        "test-model",
                    ),
                    7.5,
                )

                self.assertIsInstance(provider, OpenAICompatibleProvider)
                capabilities = provider.capabilities
                self.assertEqual(
                    expected,
                    (
                        capabilities.native_tool_calling,
                        capabilities.strict_tool_schema,
                        capabilities.parallel_tool_calls,
                        capabilities.forced_tool_choice,
                        capabilities.streaming,
                    ),
                )

    def test_factory_rejects_unregistered_provider(self) -> None:
        """防止拼写错误的厂商静默退回不支持工具调用的默认档案。"""
        config = ProviderConfig(
            "unknown",
            "test-key",
            "https://example.test/v1",
            "test-model",
        )

        with self.assertRaisesRegex(ProviderError, "未注册.*unknown"):
            create_provider(config, 3)

    def test_direct_provider_construction_rejects_unregistered_provider(self) -> None:
        """防止调用方绕过 Factory 后静默获得无能力的默认档案。"""
        config = ProviderConfig(
            "unknown",
            "test-key",
            "https://example.test/v1",
            "test-model",
        )

        with self.assertRaisesRegex(ProviderError, "未注册.*unknown"):
            OpenAICompatibleProvider(config)

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

    def test_urllib_transport_normalizes_invalid_utf8_without_response_text(
        self,
    ) -> None:
        """防止非法响应字节以 UnicodeDecodeError 或自由文本逃出 Provider 边界。"""
        transport = UrllibTransport()
        url_sentinel = "URL-QUERY-PRIVATE"
        auth_sentinel = "AUTHORIZATION-PRIVATE"
        body_sentinel = "BODY-BYTES-PRIVATE"

        with patch(
            "tricoder.providers.urllib.request.urlopen",
            return_value=FakeHttpResponse(
                body_sentinel.encode("ascii") + b"\xff",
            ),
        ):
            try:
                transport.post_json(
                    f"https://example.test/chat/completions?trace={url_sentinel}",
                    {"Authorization": f"Bearer {auth_sentinel}"},
                    {"model": "test-model", "messages": []},
                    3,
                )
            except Exception as exc:
                caught = exc
            else:
                self.fail("非法 UTF-8 响应必须失败")

        self.assertIsInstance(caught, ProviderProtocolError)
        self.assertFalse(caught.retryable)  # type: ignore[attr-defined]
        self.assertEqual("模型服务响应编码无效", str(caught))
        for sentinel in (url_sentinel, auth_sentinel, body_sentinel):
            self.assertNotIn(sentinel, str(caught))

    def test_urllib_transport_normalizes_body_read_failure_without_private_text(
        self,
    ) -> None:
        """防止响应读取异常原文、URL 或鉴权信息逃出 Provider 边界。"""
        transport = UrllibTransport()
        url_sentinel = "URL-QUERY-PRIVATE"
        auth_sentinel = "AUTHORIZATION-PRIVATE"
        failure_sentinel = "READ-FAILURE-PRIVATE"

        with patch(
            "tricoder.providers.urllib.request.urlopen",
            return_value=ReadFailingHttpResponse(
                f"{failure_sentinel} {url_sentinel} {auth_sentinel}",
            ),
        ):
            try:
                transport.post_json(
                    f"https://example.test/chat/completions?trace={url_sentinel}",
                    {"Authorization": f"Bearer {auth_sentinel}"},
                    {"model": "test-model", "messages": []},
                    3,
                )
            except Exception as exc:
                caught = exc
            else:
                self.fail("响应读取失败必须归一化")

        self.assertIsInstance(caught, ProviderError)
        self.assertNotIsInstance(caught, ProviderProtocolError)
        self.assertTrue(caught.retryable)  # type: ignore[attr-defined]
        self.assertEqual("模型服务连接或响应读取失败", str(caught))
        for sentinel in (url_sentinel, auth_sentinel, failure_sentinel):
            self.assertNotIn(sentinel, str(caught))

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
                                "arguments": '{"path":"README.md"}',
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

    def test_serializes_tools_in_name_order_regardless_of_input_order(self) -> None:
        """防止相同工具集因调用方传入顺序不同而产生不同请求前缀。"""
        alpha_tool = ToolDefinition(
            name="alpha",
            description="alphabetically first",
            parameters={"type": "object", "properties": {}},
        )
        provider, transport = make_provider(
            "openai",
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )
        transport.responses.append(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

        provider.complete([Message("user", "hello")], [WEATHER_TOOL, alpha_tool])
        provider.complete([Message("user", "hello")], [alpha_tool, WEATHER_TOOL])

        first_tools = transport.calls[0]["payload"]["tools"]  # type: ignore[index]
        second_tools = transport.calls[1]["payload"]["tools"]  # type: ignore[index]
        self.assertEqual(first_tools, second_tools)
        self.assertEqual(
            ["alpha", "weather"],
            [tool["function"]["name"] for tool in first_tools],  # type: ignore[index,union-attr]
        )

    def test_serializes_tool_call_arguments_with_stable_key_order(self) -> None:
        """防止 ToolCall 参数的插入顺序改变发送给 Provider 的 JSON 字符串。"""
        first_message = Message(
            "assistant",
            None,
            tool_calls=(ToolCall("call-1", "weather", {"city": "北京", "unit": "c"}),),
        )
        second_message = Message(
            "assistant",
            None,
            tool_calls=(ToolCall("call-1", "weather", {"unit": "c", "city": "北京"}),),
        )

        first_arguments = OpenAICompatibleProvider._serialize_message(first_message)[
            "tool_calls"
        ][0]["function"]["arguments"]  # type: ignore[index]
        second_arguments = OpenAICompatibleProvider._serialize_message(second_message)[
            "tool_calls"
        ][0]["function"]["arguments"]  # type: ignore[index]

        self.assertEqual(first_arguments, second_arguments)
        self.assertEqual('{"city":"北京","unit":"c"}', first_arguments)

    def test_stable_json_bytes_uses_sorted_utf8_compact_encoding(self) -> None:
        """防止字典插入顺序或 ASCII 转义改变 Provider 的请求字节。"""
        first_payload = {"模型": "测试", "options": {"b": 2, "a": 1}}
        second_payload = {"options": {"a": 1, "b": 2}, "模型": "测试"}

        first_bytes = _stable_json_bytes(first_payload)
        second_bytes = _stable_json_bytes(second_payload)

        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            b'{"options":{"a":1,"b":2},"\xe6\xa8\xa1\xe5\x9e\x8b":"\xe6\xb5\x8b\xe8\xaf\x95"}',
            first_bytes,
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
            "deepseek": {
                "thinking": {"type": "disabled"},
                "tool_choice": "auto",
            },
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
                payload = transport.calls[0]["payload"]
                self.assertEqual(
                    [{"type": "function", "function": expected_function}],
                    payload["tools"],  # type: ignore[index]
                )
                for field, value in extras.items():
                    self.assertEqual(value, payload.get(field))  # type: ignore[union-attr]
                self.assertEqual(
                    provider_name == "openai",
                    "parallel_tool_calls" in payload,
                )

    def test_openai_omits_strict_for_real_registry_optional_schemas(self) -> None:
        """防止真实可选参数 Schema 被错误声明为 OpenAI strict 工具。"""
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(Path(directory)),
                    CommandPolicy(),
                    approver=lambda _action, _detail: False,
                )
            )
            definitions = registry.definitions
            optional_fields = {
                f"{definition.name}.{name}"
                for definition in definitions
                for name in definition.parameters["properties"]
                if name not in definition.parameters["required"]
            }
            provider, transport = make_provider(
                "openai",
                {
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ]
                },
            )

            provider.complete([Message("user", "检查工作区")], definitions)

        self.assertEqual(
            {"list_files.path", "search_text.path", "run_command.cwd"},
            optional_fields,
        )
        payload = transport.calls[0]["payload"]
        serialized_tools = payload["tools"]  # type: ignore[index]
        self.assertEqual(
            sorted(definition.name for definition in definitions),
            [
                tool["function"]["name"]  # type: ignore[index]
                for tool in serialized_tools  # type: ignore[union-attr]
            ],
        )
        for tool in serialized_tools:  # type: ignore[union-attr]
            self.assertNotIn("strict", tool["function"])  # type: ignore[index]

    def test_deepseek_disables_thinking_for_provider_neutral_tool_rounds(self) -> None:
        """防止 DeepSeek thinking 要求泄漏进共享消息模型或破坏自动工具选择。"""
        provider, transport = make_provider(
            "deepseek",
            {
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )
        messages = [
            Message("user", "读取天气"),
            Message(
                "assistant",
                None,
                tool_calls=(
                    ToolCall("call-weather", "weather", {"city": "深圳"}),
                ),
            ),
            Message("tool", "晴", tool_call_id="call-weather"),
        ]

        provider.complete(messages, [WEATHER_TOOL])

        payload = transport.calls[0]["payload"]
        self.assertEqual(
            {"type": "disabled"},
            payload.get("thinking"),  # type: ignore[union-attr]
        )
        self.assertEqual("auto", payload["tool_choice"])  # type: ignore[index]
        self.assertEqual(
            [
                {"role": "user", "content": "读取天气"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-weather",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"深圳"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "晴",
                    "tool_call_id": "call-weather",
                },
            ],
            payload["messages"],  # type: ignore[index]
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
                "thinking": {"type": "disabled"},
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
