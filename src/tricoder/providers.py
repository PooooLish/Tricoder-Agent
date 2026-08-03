"""OpenAI-compatible 模型服务适配。"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from tricoder.models import (
    Message,
    ProviderConfig,
    ProviderResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


class ProviderError(RuntimeError):
    """统一表示模型请求失败，并标记是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProviderProtocolError(ProviderError):
    """厂商响应能收到，但无法转换成统一协议。"""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """单个厂商已声明支持的原生模型能力。"""

    native_tool_calling: bool
    strict_tool_schema: bool = False
    parallel_tool_calls: bool = False
    forced_tool_choice: bool = False
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class _ProviderProfile:
    """将厂商请求差异封装在 Provider 层内。"""

    capabilities: ProviderCapabilities
    usage_dialect: str
    automatic_tool_choice: bool = False
    disable_thinking: bool = False


_PROVIDER_PROFILES = {
    "openai": _ProviderProfile(
        ProviderCapabilities(
            native_tool_calling=True,
            parallel_tool_calls=True,
            forced_tool_choice=True,
            streaming=True,
        ),
        usage_dialect="openai",
        automatic_tool_choice=True,
    ),
    "deepseek": _ProviderProfile(
        ProviderCapabilities(
            native_tool_calling=True,
            forced_tool_choice=True,
            streaming=True,
        ),
        usage_dialect="deepseek",
        automatic_tool_choice=True,
        disable_thinking=True,
    ),
    "glm": _ProviderProfile(
        ProviderCapabilities(
            native_tool_calling=True,
            streaming=True,
        ),
        usage_dialect="openai",
        automatic_tool_choice=True,
    ),
}
class JsonTransport(Protocol):
    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        """发送 JSON POST 请求并返回解析后的对象。"""


class UrllibTransport:
    """使用 Python 标准库实现的 HTTPS JSON 传输。"""

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise ProviderError(f"模型服务返回 HTTP {exc.code}", retryable=retryable) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise ProviderError("模型服务连接或响应读取失败", retryable=True) from exc

        try:
            raw = response_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderProtocolError("模型服务响应编码无效") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderProtocolError("模型服务返回了无效 JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderProtocolError("模型服务响应格式不正确")
        return decoded


class ModelProvider(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        """根据完整消息历史和工具定义生成统一响应。"""


class OpenAICompatibleProvider:
    """调用三家服务共同支持的 Chat Completions 协议子集。"""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: JsonTransport | None = None,
        timeout: float = 30,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        _profile: _ProviderProfile | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibTransport()
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        if _profile is None:
            try:
                _profile = _PROVIDER_PROFILES[config.name.casefold()]
            except KeyError as exc:
                raise ProviderError(f"未注册的模型服务：{config.name}") from exc
        self._profile = _profile

    @property
    def capabilities(self) -> ProviderCapabilities:
        """公开不可变的厂商能力快照。"""

        return self._profile.capabilities

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [self._serialize_message(message) for message in messages],
        }
        if self._profile.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        if tools and self.capabilities.native_tool_calling:
            payload["tools"] = [self._serialize_tool(tool) for tool in tools]
            if self._profile.automatic_tool_choice:
                payload["tool_choice"] = "auto"
            if self.capabilities.parallel_tool_calls:
                payload["parallel_tool_calls"] = False
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"

        for attempt in range(self._max_attempts):
            try:
                response = self._transport.post_json(
                    url,
                    headers,
                    payload,
                    self._timeout,
                )
                return self._extract_response(response)
            except ProviderError as exc:
                if not exc.retryable or attempt == self._max_attempts - 1:
                    raise
                self._sleeper(float(2**attempt))
        raise ProviderError("模型请求重试逻辑异常")

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, object]:
        payload: dict[str, object] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    def _serialize_tool(self, tool: ToolDefinition) -> dict[str, object]:
        function: dict[str, object] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if self.capabilities.strict_tool_schema:
            function["strict"] = True
        return {"type": "function", "function": function}

    def _extract_response(self, response: dict[str, object]) -> ProviderResponse:
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            first = choices[0]
            if not isinstance(first, dict):
                raise TypeError
            message = first["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message.get("content")
            raw_tool_calls = message.get("tool_calls", [])
            if not isinstance(raw_tool_calls, list):
                raise TypeError
            tool_calls = tuple(
                self._parse_tool_call(raw_call) for raw_call in raw_tool_calls
            )
            if content is not None and not isinstance(content, str):
                raise TypeError
            if content is None and not tool_calls:
                raise TypeError
            finish_reason = first.get("finish_reason")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise TypeError
            return ProviderResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=self._extract_usage(response),
            )
        except ProviderProtocolError:
            raise
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ProviderProtocolError("模型服务响应格式不正确") from exc

    def _extract_usage(self, response: dict[str, object]) -> TokenUsage | None:
        raw_usage = response.get("usage")
        if not isinstance(raw_usage, dict):
            return None

        input_tokens = _optional_token_count(raw_usage.get("prompt_tokens"))
        output_tokens = _optional_token_count(raw_usage.get("completion_tokens"))
        cached_tokens: int | None = None
        cache_miss_tokens: int | None = None
        if self._profile.usage_dialect == "deepseek":
            cached_tokens = _optional_token_count(
                raw_usage.get("prompt_cache_hit_tokens")
            )
            cache_miss_tokens = _optional_token_count(
                raw_usage.get("prompt_cache_miss_tokens")
            )
        else:
            details = raw_usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                cached_tokens = _optional_token_count(details.get("cached_tokens"))

        if all(
            value is None
            for value in (
                input_tokens,
                output_tokens,
                cached_tokens,
                cache_miss_tokens,
            )
        ):
            return None
        return TokenUsage(
            input_tokens,
            output_tokens,
            cached_tokens,
            cache_miss_tokens,
        )

    @staticmethod
    def _parse_tool_call(raw_call: object) -> ToolCall:
        try:
            if not isinstance(raw_call, dict):
                raise TypeError
            call_id = raw_call["id"]
            if raw_call.get("type") != "function":
                raise TypeError
            function = raw_call["function"]
            if not isinstance(call_id, str) or not call_id:
                raise TypeError
            if not isinstance(function, dict):
                raise TypeError
            name = function["name"]
            arguments_json = function["arguments"]
            if not isinstance(name, str) or not name:
                raise TypeError
            if not isinstance(arguments_json, str):
                raise TypeError
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise TypeError
            return ToolCall(id=call_id, name=name, arguments=arguments)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderProtocolError("模型服务工具调用格式不正确") from exc


def _optional_token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _openai_compatible_factory(
    profile: _ProviderProfile,
) -> Callable[[ProviderConfig, float], ModelProvider]:
    """为注册表绑定厂商档案，避免协议选择泄漏到调用层。"""

    def build(config: ProviderConfig, timeout: float) -> ModelProvider:
        return OpenAICompatibleProvider(config, timeout=timeout, _profile=profile)

    return build


# 注册表值是统一接口工厂，未来可替换为完全不同的 Provider 实现。
_PROVIDER_FACTORIES = {
    name: _openai_compatible_factory(profile)
    for name, profile in _PROVIDER_PROFILES.items()
}


def create_provider(config: ProviderConfig, timeout: float) -> ModelProvider:
    """根据配置选择已注册的 Provider 实现。"""

    provider_name = config.name.casefold()
    try:
        factory = _PROVIDER_FACTORIES[provider_name]
    except KeyError as exc:
        raise ProviderError(f"未注册的模型服务：{config.name}") from exc
    return factory(config, timeout)
