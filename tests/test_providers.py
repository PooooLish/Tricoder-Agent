import unittest

from tricoder.models import Message, ProviderConfig
from tricoder.providers import OpenAICompatibleProvider, ProviderError


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


class ProviderTests(unittest.TestCase):
    def test_builds_openai_compatible_request(self) -> None:
        """防止地址、鉴权头或消息结构偏离三家兼容协议的公共子集。"""
        transport = RecordingTransport(
            [{"choices": [{"message": {"content": '{"tool":"finish"}'}}]}]
        )
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                name="deepseek",
                api_key="test-secret",
                base_url="https://api.deepseek.com/",
                model="deepseek-v4-flash",
            ),
            transport=transport,
            timeout=9,
            sleeper=lambda _: None,
        )

        content = provider.complete([Message(role="user", content="修复测试")])

        self.assertEqual('{"tool":"finish"}', content)
        self.assertEqual("https://api.deepseek.com/chat/completions", transport.calls[0]["url"])
        self.assertEqual(
            "Bearer test-secret",
            transport.calls[0]["headers"]["Authorization"],  # type: ignore[index]
        )
        self.assertEqual(
            {
                "model": "deepseek-v4-flash",
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
                {"choices": [{"message": {"content": "ok"}}]},
            ]
        )
        delays: list[float] = []
        provider = OpenAICompatibleProvider(
            ProviderConfig("openai", "test-key", "https://api.openai.com/v1", "gpt-5"),
            transport=transport,
            timeout=10,
            sleeper=delays.append,
        )

        self.assertEqual("ok", provider.complete([Message("user", "hello")]))
        self.assertEqual(3, len(transport.calls))
        self.assertEqual([1.0, 2.0], delays)

    def test_rejects_malformed_response(self) -> None:
        """防止上游异常响应变成难以定位的下标错误。"""
        provider = OpenAICompatibleProvider(
            ProviderConfig("glm", "test-key", "https://example.test/v4", "glm-5.2"),
            transport=RecordingTransport([{"choices": []}]),
            sleeper=lambda _: None,
        )

        with self.assertRaisesRegex(ProviderError, "响应格式"):
            provider.complete([Message("user", "hello")])


if __name__ == "__main__":
    unittest.main()
