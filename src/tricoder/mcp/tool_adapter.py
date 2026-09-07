"""MCP 工具名称与结果的纯转换层。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tricoder.core.cancellation import CancellationToken
from tricoder.models import ToolResult
from tricoder.tools.handlers import ToolHandler

from .models import MAX_RESULT_CHARS, MCPCallResult

if TYPE_CHECKING:
    from .manager import MCPManager
    from .models import MCPToolSpec
    from tricoder.tools import ToolContext


_UNSAFE_NAME_RUN = re.compile(r"[^a-z0-9_]+")
_UNDERSCORE_RUN = re.compile(r"_+")
_OMITTED_CONTENT_TYPES = {
    "image": "image",
    "audio": "audio",
    "resource": "resource",
    "resource_link": "resource",
    "embedded_resource": "resource",
    "blob": "blob",
}


class MCPToolHandler(ToolHandler):
    """把一个已验证 MCP 工具接入现有异步 Tool Gateway。"""

    risk = "dangerous"

    def __init__(
        self,
        context: "ToolContext",
        manager: "MCPManager",
        spec: "MCPToolSpec",
    ) -> None:
        super().__init__(context)
        self._manager = manager
        self.server_id = spec.server_id
        self.raw_name = spec.raw_name
        self.name = spec.public_name
        self.description = spec.description
        self.parameters = spec.input_schema

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """同步入口不得为 MCP 调用创建嵌套事件循环。"""

        return ToolResult(False, "MCP 工具仅支持异步执行")

    async def run_async(
        self,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        """在当前事件循环中经 manager 的 exact 路由调用 MCP server。"""

        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        result = await self._manager.call_tool(
            self.server_id,
            self.raw_name,
            arguments,
            token,
        )
        return ToolResult(result.ok, result.text)


def normalize_tool_name(server_id: str, raw_name: str) -> str:
    """生成 ToolRegistry 可接受且跨进程稳定的 MCP 工具名。"""

    if not isinstance(server_id, str) or not isinstance(raw_name, str):
        raise TypeError("MCP server id 和工具名必须是字符串")
    server_component = _normalize_name_component(server_id)
    tool_component = _normalize_name_component(raw_name)
    candidate = f"mcp__{server_component}__{tool_component}"
    if len(candidate) <= 64:
        return candidate

    digest = hashlib.sha256(f"{server_id}\0{raw_name}".encode("utf-8")).hexdigest()[:10]
    prefix = candidate[: 64 - len(digest) - 1].rstrip("_")
    return f"{prefix}_{digest}"


def _normalize_name_component(value: str) -> str:
    component = _UNSAFE_NAME_RUN.sub("_", value.lower())
    component = _UNDERSCORE_RUN.sub("_", component).strip("_")
    if not component:
        return "x"
    if not component[0].isalpha() or not component[0].isascii():
        component = f"x_{component}"
    return component


def normalize_mcp_result(
    result: object,
    *,
    max_chars: int = MAX_RESULT_CHARS,
) -> MCPCallResult:
    """只提取白名单文本，并用无 payload 占位符表示其他内容。"""

    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 0:
        raise ValueError("max_chars 必须是非负整数")
    limit = min(max_chars, MAX_RESULT_CHARS)
    content = _safe_field(result, "content")
    blocks = content if isinstance(content, (list, tuple)) else ()
    parts: list[str] = []
    used = 0
    omitted: list[str] = []

    def append_text(text: str) -> None:
        nonlocal used
        if used >= limit:
            return
        separator = "\n" if parts else ""
        available = limit - used
        if separator:
            if available <= 1:
                return
            available -= 1
        chunk = text[:available]
        if not chunk:
            return
        parts.append(f"{separator}{chunk}")
        used += len(separator) + len(chunk)

    def record_omitted(content_type: str) -> None:
        if content_type not in omitted:
            omitted.append(content_type)

    for block in blocks:
        content_type = _safe_field(block, "type")
        if content_type == "text":
            text = _safe_field(block, "text")
            if isinstance(text, str):
                append_text(text)
            else:
                record_omitted("unknown")
            continue
        if content_type in _OMITTED_CONTENT_TYPES:
            normalized_type = _OMITTED_CONTENT_TYPES[content_type]
            record_omitted(normalized_type)
            append_text(f"[已省略 MCP {normalized_type} 内容]")
            continue
        record_omitted("unknown")

    return MCPCallResult(
        ok=not _result_is_error(result),
        text="".join(parts),
        omitted_content_types=tuple(omitted),
    )


def _result_is_error(result: object) -> bool:
    """对象优先 Python 字段，mapping 优先 wire alias；只接受真实布尔值。"""

    primary, fallback = (
        ("isError", "is_error")
        if isinstance(result, Mapping)
        else ("is_error", "isError")
    )
    value = _safe_field(result, primary)
    if isinstance(value, bool):
        return value
    fallback_value = _safe_field(result, fallback)
    return fallback_value is True


def _safe_field(value: object, name: str) -> object:
    """只读取已知字段；失败时绝不格式化未知对象或异常正文。"""

    if isinstance(value, Mapping):
        try:
            return value.get(name)
        except Exception:
            return None
    try:
        return getattr(value, name)
    except Exception:
        return None
