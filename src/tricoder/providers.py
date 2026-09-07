"""OpenAI-compatible 模型服务适配。"""

from __future__ import annotations

import asyncio
import codecs
import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# 模型响应体上限：防止恶意/异常响应拖垮内存，超限只抛不含正文的错误。
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_STREAM_EVENT_BYTES = 1024 * 1024
_MAX_STREAM_OUTPUT_BYTES = 8 * 1024 * 1024
_STREAM_READ_BYTES = 64 * 1024
from typing import AsyncIterator, Callable, Protocol

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import (
    ProviderCompleted,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolCallCompleted,
    UsageReported,
)
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


class StreamTransport(Protocol):
    def post_stream(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[bytes]:
        """发送 JSON POST 请求并逐块返回 SSE 响应字节。"""


def _stable_json_bytes(payload: dict[str, object]) -> bytes:
    """以固定键顺序和 UTF-8 编码生成可复用的请求字节。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
            data=_stable_json_bytes(payload),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                chunk = response.read(_MAX_RESPONSE_BYTES + 1)
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
        if len(chunk) > _MAX_RESPONSE_BYTES:
            # 只报超限，绝不把响应正文带进异常，防止凭据/大文本外泄。
            raise ProviderProtocolError("模型服务响应超过字节上限")
        response_body = chunk

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

    async def post_stream(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[bytes]:
        """在线程中执行阻塞 urllib 读取，向异步调用方交付有界字节块。"""

        request = urllib.request.Request(
            url,
            data=_stable_json_bytes(payload),
            headers=headers,
            method="POST",
        )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        try:
            response = await asyncio.to_thread(
                urllib.request.urlopen,
                request,
                timeout=timeout,
            )
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
            read_chunk = getattr(response, "read1", None)
            if not callable(read_chunk):
                read_chunk = response.read
            while True:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                try:
                    chunk = await asyncio.to_thread(read_chunk, _STREAM_READ_BYTES)
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    http.client.HTTPException,
                ) as exc:
                    raise ProviderError(
                        "模型服务连接或响应读取失败",
                        retryable=True,
                    ) from exc
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ProviderProtocolError("模型服务流返回了无效字节块")
                yield chunk
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                await asyncio.to_thread(close)


class ModelProvider(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        """根据完整消息历史和工具定义生成统一响应。"""

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
        *,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """逐步生成 Provider 无关的类型化事件。"""


class OpenAICompatibleProvider:
    """调用三家服务共同支持的 Chat Completions 协议子集。"""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: JsonTransport | StreamTransport | None = None,
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
        payload, headers, url = self._build_request(messages, tools)

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

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
        *,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """把 OpenAI-compatible SSE 转换成完整、安全的内部事件。"""

        payload, headers, url = self._build_request(messages, tools)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        transport = self._transport
        post_stream = getattr(transport, "post_stream", None)
        if not callable(post_stream):
            raise ProviderError("当前传输层不支持流式响应")

        for attempt in range(self._max_attempts):
            emitted = False
            try:
                async for event in self._stream_once(
                    post_stream(url, headers, payload, self._timeout, cancellation),
                    cancellation,
                ):
                    emitted = True
                    yield event
                return
            except CancellationError:
                raise
            except ProviderError as exc:
                if emitted or not exc.retryable or attempt == self._max_attempts - 1:
                    raise
                delay = float(2**attempt)
                if cancellation is None:
                    await asyncio.to_thread(self._sleeper, delay)
                else:
                    cancellation.raise_if_cancelled()
                    if await asyncio.to_thread(cancellation.wait, delay):
                        raise CancellationError("操作已取消")
        raise ProviderError("模型请求重试逻辑异常")

    def _build_request(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...],
    ) -> tuple[dict[str, object], dict[str, str], str]:
        """构造同步和流式入口共享的稳定请求。"""

        tool_names = [tool.name for tool in tools]
        if len(set(tool_names)) != len(tool_names):
            raise ProviderError("工具名称必须唯一")

        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [self._serialize_message(message) for message in messages],
        }
        if self._profile.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        if tools and self.capabilities.native_tool_calling:
            ordered_tools = sorted(tools, key=lambda tool: tool.name)
            payload["tools"] = [self._serialize_tool(tool) for tool in ordered_tools]
            if self._profile.automatic_tool_choice:
                payload["tool_choice"] = "auto"
            if self.capabilities.parallel_tool_calls:
                payload["parallel_tool_calls"] = False
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        return (
            payload,
            headers,
            f"{self._config.base_url.rstrip('/')}/chat/completions",
        )

    async def _stream_once(
        self,
        byte_stream: AsyncIterator[bytes],
        cancellation: CancellationToken | None,
    ) -> AsyncIterator[ProviderEvent]:
        """解析单次 SSE 响应；只有完成边界才能发布工具调用。"""

        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text_buffer = ""
        data_lines: list[str] = []
        data_bytes = 0
        total_bytes = 0
        output_bytes = 0
        completed = False
        finish_seen = False
        finish_reason_value: str | None = None
        tools_finalized = False
        pending_tools: dict[int, dict[str, str]] = {}
        started_tools: set[int] = set()

        def append_data_line(value: str) -> None:
            """增量计算事件大小，避免对大量 data 行反复 join 形成平方开销。"""
            nonlocal data_bytes
            data_bytes += len(value.encode("utf-8")) + (1 if data_lines else 0)
            if data_bytes > _MAX_STREAM_EVENT_BYTES:
                raise ProviderProtocolError("模型服务流单事件超过字节上限")
            data_lines.append(value)

        def count_output(value: str) -> None:
            nonlocal output_bytes
            output_bytes += len(value.encode("utf-8"))
            if output_bytes > _MAX_STREAM_OUTPUT_BYTES:
                raise ProviderProtocolError("模型服务流输出超过累计上限")

        def parse_data(data: str) -> tuple[ProviderEvent, ...]:
            nonlocal completed, finish_seen, finish_reason_value, tools_finalized
            if data == "[DONE]":
                if completed:
                    return ()
                events = [] if tools_finalized else list(finalize_tools())
                tools_finalized = True
                completed = True
                events.append(ProviderCompleted(finish_reason_value))
                return tuple(events)
            try:
                decoded = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError("模型服务流事件 JSON 无效") from exc
            if not isinstance(decoded, dict):
                raise ProviderProtocolError("模型服务流事件格式不正确")

            events: list[ProviderEvent] = []
            usage = self._extract_usage(decoded)
            if usage is not None:
                events.append(UsageReported(usage))

            if "choices" not in decoded:
                return tuple(events)
            choices = decoded["choices"]
            if not isinstance(choices, list):
                raise ProviderProtocolError("模型服务流事件格式不正确")
            if not choices:
                return tuple(events)
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ProviderProtocolError("模型服务流事件格式不正确")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ProviderProtocolError("模型服务流事件格式不正确")

            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ProviderProtocolError("模型服务流文本格式不正确")
                count_output(content)
                if content:
                    events.append(TextDelta(content))
            thinking = delta.get("reasoning_content", delta.get("reasoning"))
            if thinking is not None:
                if not isinstance(thinking, str):
                    raise ProviderProtocolError("模型服务流思考格式不正确")
                count_output(thinking)
                if thinking:
                    events.append(ThinkingDelta(thinking))

            raw_calls = delta.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise ProviderProtocolError("模型服务流工具调用格式不正确")
            if raw_calls and tools_finalized:
                raise ProviderProtocolError("模型服务流在结束后继续生成工具调用")
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    raise ProviderProtocolError("模型服务流工具调用格式不正确")
                index = raw_call.get("index")
                if type(index) is not int or index < 0:
                    raise ProviderProtocolError("模型服务流工具调用格式不正确")
                pending = pending_tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                call_id = raw_call.get("id")
                if call_id is not None:
                    if not isinstance(call_id, str) or (pending["id"] and pending["id"] != call_id):
                        raise ProviderProtocolError("模型服务流工具调用格式不正确")
                    pending["id"] = call_id
                call_type = raw_call.get("type")
                if call_type is not None and call_type != "function":
                    raise ProviderProtocolError("模型服务流工具调用格式不正确")
                function = raw_call.get("function", {})
                if not isinstance(function, dict):
                    raise ProviderProtocolError("模型服务流工具调用格式不正确")
                name = function.get("name")
                if name is not None:
                    if not isinstance(name, str) or (pending["name"] and pending["name"] != name):
                        raise ProviderProtocolError("模型服务流工具调用格式不正确")
                    pending["name"] = name
                arguments = function.get("arguments")
                if arguments is not None:
                    if not isinstance(arguments, str):
                        raise ProviderProtocolError("模型服务流工具调用格式不正确")
                    count_output(arguments)
                    pending["arguments"] += arguments
                if pending["id"] and pending["name"] and index not in started_tools:
                    started_tools.add(index)
                    events.append(ToolCallStarted(pending["id"], pending["name"]))

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str):
                    raise ProviderProtocolError("模型服务流结束原因格式不正确")
                if finish_seen:
                    raise ProviderProtocolError("模型服务流重复报告结束原因")
                events.extend(finalize_tools())
                tools_finalized = True
                finish_seen = True
                finish_reason_value = finish_reason
            return tuple(events)

        def finalize_tools() -> tuple[ToolCallCompleted, ...]:
            completed_calls: list[ToolCallCompleted] = []
            for index in sorted(pending_tools):
                pending = pending_tools[index]
                if not pending["id"] or not pending["name"]:
                    raise ProviderProtocolError("模型服务流工具调用不完整")
                try:
                    arguments = json.loads(pending["arguments"])
                except json.JSONDecodeError as exc:
                    raise ProviderProtocolError("模型服务流工具调用格式不正确") from exc
                if not isinstance(arguments, dict):
                    raise ProviderProtocolError("模型服务流工具调用格式不正确")
                completed_calls.append(
                    ToolCallCompleted(ToolCall(pending["id"], pending["name"], arguments))
                )
            pending_tools.clear()
            return tuple(completed_calls)

        async_iterator = byte_stream.__aiter__()
        try:
            while True:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                try:
                    chunk = await anext(async_iterator)
                except StopAsyncIteration:
                    break
                except CancellationError:
                    raise
                except ProviderError:
                    raise
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    http.client.HTTPException,
                ) as exc:
                    raise ProviderError(
                        "模型服务连接或响应读取失败",
                        retryable=True,
                    ) from exc
                if not isinstance(chunk, bytes):
                    raise ProviderProtocolError("模型服务流返回了无效字节块")
                total_bytes += len(chunk)
                if total_bytes > _MAX_RESPONSE_BYTES:
                    raise ProviderProtocolError("模型服务流响应超过字节上限")
                try:
                    text_buffer += decoder.decode(chunk)
                except UnicodeDecodeError as exc:
                    raise ProviderProtocolError("模型服务流响应编码无效") from exc

                while "\n" in text_buffer:
                    line, text_buffer = text_buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    if line == "":
                        if data_lines:
                            data = "\n".join(data_lines)
                            data_lines.clear()
                            data_bytes = 0
                            for event in parse_data(data):
                                yield event
                        continue
                    if line.startswith(":"):
                        continue
                    if line == "data":
                        append_data_line("")
                    elif line.startswith("data:"):
                        value = line[5:]
                        append_data_line(
                            value[1:] if value.startswith(" ") else value
                        )
        finally:
            aclose = getattr(async_iterator, "aclose", None)
            if callable(aclose):
                await aclose()

        try:
            text_buffer += decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ProviderProtocolError("模型服务流响应编码无效") from exc
        if text_buffer:
            line = text_buffer.rstrip("\r")
            if line.startswith("data:"):
                value = line[5:]
                append_data_line(value[1:] if value.startswith(" ") else value)
        if data_lines:
            data = "\n".join(data_lines)
            for event in parse_data(data):
                yield event
        if not completed:
            raise ProviderError("模型服务流意外中断", retryable=True)

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
                            sort_keys=True,
                            separators=(",", ":"),
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
        direct_cached_tokens = _optional_token_count(
            raw_usage.get("prompt_cache_hit_tokens")
        )
        cache_miss_tokens = _optional_token_count(
            raw_usage.get("prompt_cache_miss_tokens")
        )
        nested_cached_tokens: int | None = None
        details = raw_usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            nested_cached_tokens = _optional_token_count(details.get("cached_tokens"))

        if self._profile.usage_dialect == "deepseek":
            cached_tokens = (
                direct_cached_tokens
                if direct_cached_tokens is not None
                else nested_cached_tokens
            )
        else:
            cached_tokens = (
                nested_cached_tokens
                if nested_cached_tokens is not None
                else direct_cached_tokens
            )

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
