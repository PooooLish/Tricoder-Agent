"""OpenAI-compatible 模型服务适配。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

from tricoder.models import Message, ProviderConfig


class ProviderError(RuntimeError):
    """统一表示模型请求失败，并标记是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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
            raise ProviderError("模型服务返回了无效 JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderError("模型服务响应格式不正确")
        return decoded


class ModelProvider(Protocol):
    def complete(self, messages: list[Message]) -> str:
        """根据完整消息历史生成下一步文本。"""


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

    def complete(self, messages: list[Message]) -> str:
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [message.as_dict() for message in messages],
        }
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
                return self._extract_content(response)
            except ProviderError as exc:
                if not exc.retryable or attempt == self._max_attempts - 1:
                    raise
                self._sleeper(float(2**attempt))
        raise ProviderError("模型请求重试逻辑异常")

    @staticmethod
    def _extract_content(response: dict[str, object]) -> str:
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
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError
            return content
        except (KeyError, TypeError, IndexError) as exc:
            raise ProviderError("模型服务响应格式不正确") from exc
