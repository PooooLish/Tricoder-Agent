"""消息历史完整回合识别和字符预算兼容入口。"""

from __future__ import annotations

from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.models import Message
from tricoder.protocols import _PROTOCOLS


def is_complete_tool_round(
    assistant: Message,
    tool_result: Message,
    tool_protocol: str | None = None,
) -> bool:
    """按指定协议或全部协议的宽松语义校验一个工具回合。"""

    if assistant.role != "assistant":
        return False
    if tool_protocol is None:
        return any(
            protocol.complete_round_loose(assistant, tool_result)
            for protocol in _PROTOCOLS.values()
        )
    return _PROTOCOLS[tool_protocol].complete_round(assistant, tool_result)


def complete_round_tail(
    messages: list[Message],
    index: int,
    tool_protocol: str | None = None,
) -> int | None:
    """返回从 ``index`` 开始的完整工具回合结束下标，否则返回 ``None``。"""

    assistant = messages[index]
    if assistant.role != "assistant":
        return None
    if assistant.tool_calls:
        if tool_protocol not in {None, "native"}:
            return None
        cursor = index + 1
        for call in assistant.tool_calls:
            if cursor >= len(messages):
                return None
            result = messages[cursor]
            if (
                result.role != "tool"
                or result.kind != "tool_result"
                or result.tool_call_id != call.id
            ):
                return None
            cursor += 1
        return cursor
    if tool_protocol not in {None, "legacy_json"}:
        return None
    if index + 1 >= len(messages):
        return None
    result = messages[index + 1]
    if result.role != "user":
        return None
    if tool_protocol == "legacy_json" and result.kind != "tool_result":
        return None
    return index + 2


def compact_messages(messages: list[Message], max_chars: int) -> list[Message]:
    """按完整回合压缩通用消息历史，保留旧字符预算接口。"""

    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_context_chars 必须大于 0")
    manager = ContextManager(
        ContextBudget(max_chars=max_chars),
        tuple(_PROTOCOLS.values()),
    )
    return list(manager.prepare_generic(messages).messages)


def compact_session_messages(
    messages: list[Message],
    max_chars: int,
    tool_protocol: str,
) -> list[Message]:
    """按指定会话协议压缩历史，保持旧调用参数和返回类型。"""

    manager = ContextManager(
        ContextBudget(max_chars=max_chars),
        _PROTOCOLS[tool_protocol],
    )
    return list(manager.prepare(messages).messages)
