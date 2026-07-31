"""OpenAI-compatible 模型服务适配。"""

from __future__ import annotations

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
    automatic_tool_choice: bool = False


_PROVIDER_PROFILES = {
    "openai": _ProviderProfile(
        ProviderCapabilities(
            native_tool_calling=True,
            strict_tool_schema=True,
            parallel_tool_calls=True,
            forced_tool_choice=True,
            streaming=True,
        ),
        automatic_tool_choice=True,
    ),
    "deepseek": _ProviderProfile(
        ProviderCapabilities(
            native_tool_calling=True,
            forced_tool_choice=True,
            streaming=True,
        ),
        automatic_tool_choice=True,
    ),
    "glm": _ProviderProfile(
        ProviderCapabilities(
            native_tool_calling=True,
            streaming=True,
        ),
        automatic_tool_choice=True,
    ),
}
_DEFAULT_PROFILE = _ProviderProfile(ProviderCapabilities(native_tool_calling=False))


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
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise ProviderError(f"模型服务返回 HTTP {exc.code}", retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(f"模型服务连接失败：{exc}", retryable=True) from exc

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
    ) -> None:
        self._config = config
        self._transport = transport or UrllibTransport()
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._profile = _PROVIDER_PROFILES.get(
            config.name.casefold(),
            _DEFAULT_PROFILE,
        )

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

    @classmethod
    def _extract_response(cls, response: dict[str, object]) -> ProviderResponse:
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
                cls._parse_tool_call(raw_call) for raw_call in raw_tool_calls
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
            )
        except ProviderProtocolError:
            raise
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ProviderProtocolError("模型服务响应格式不正确") from exc

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
