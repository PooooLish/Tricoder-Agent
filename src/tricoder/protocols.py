"""Agent 动作解析协议：将模型响应归一化为动作或修正反馈。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from tricoder.models import (
    Message,
    ProviderResponse,
    ToolAction,
    ToolResult,
)


COMMON_SYSTEM_PROMPT = """你是一个在本地代码工作区内协作的 Coding Agent。
不要输出隐藏思维过程。只说明当前动作的直接目的。
编辑前必须先读取目标文件；遇到工具错误时根据错误信息调整下一步。
"""

NATIVE_TOOL_PROMPT = """使用 Provider 提供的原生工具调用完成任务。
每轮必须且只能选择一个工具；不要用普通文本或并行工具调用代替。
工具参数只包含当前工具定义允许的字段。
"""

LEGACY_JSON_PROMPT = """每轮只能返回一个 JSON 对象，不能使用 Markdown 代码块，也不能添加对象之外的文字：
{"tool":"工具名","arguments":{},"reason":"简短、可公开审计的操作理由"}

可用工具：
- list_files: {"path":"相对目录"}
- read_file: {"path":"相对文件"}
- search_text: {"path":"相对目录","query":"文本","use_regex":false}
- glob_files: {"path":"相对目录","pattern":"相对glob模式"}
- edit_file: {"path":"相对文件","old_text":"精确旧文本","new_text":"新文本"}
- create_file: {"path":"相对文件","content":"新文件完整内容"}
- run_command: {"command":"测试或静态检查命令","cwd":"可选相对目录"}
- finish: {"summary":"完成情况、验证结果和剩余风险"}
"""

SYSTEM_PROMPT = COMMON_SYSTEM_PROMPT + NATIVE_TOOL_PROMPT
LEGACY_SYSTEM_PROMPT = COMMON_SYSTEM_PROMPT + LEGACY_JSON_PROMPT

NATIVE_TEXT_FEEDBACK = "本轮没有工具调用。请下一轮只选择一个可用工具调用。"
NATIVE_MULTIPLE_CALLS_FEEDBACK = "本轮包含多个工具调用，未执行任何一个。请下一轮只选择一个。"
PROTOCOL_FEEDBACK = "模型响应未满足当前协议。请下一轮按系统规则重新提交一个动作。"


def parse_action(raw: str) -> ToolAction:
    """严格解析模型动作，拒绝模糊或缺字段的自由文本。"""

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"动作格式错误：必须返回有效 JSON（{exc.msg}）") from exc
    if not isinstance(decoded, dict):
        raise ValueError("动作格式错误：根节点必须是对象")

    tool = decoded.get("tool")
    arguments = decoded.get("arguments")
    reason = decoded.get("reason")
    if not isinstance(tool, str) or not tool:
        raise ValueError("动作格式错误：tool 必须是非空字符串")
    if not isinstance(arguments, dict):
        raise ValueError("动作格式错误：arguments 必须是对象")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("动作格式错误：reason 必须是非空字符串")
    return ToolAction(tool=tool, arguments=arguments, reason=reason.strip())


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """模型响应解析结果：assistant 消息、可执行动作或修正反馈。"""

    assistant_messages: tuple[Message, ...]
    action: ToolAction | None = None
    feedback: Message | None = None
    audit_error_type: str | None = None
    tool_call_id: str | None = None


class ActionProtocol(Protocol):
    """将模型响应解析为动作或反馈；新增协议只需实现并注册。"""

    system_prompt: str
    tools_enabled: bool

    def resolve_action(self, response: ProviderResponse) -> ResolvedAction:
        """解析响应为 assistant 消息、可执行动作或修正反馈。"""

    def tool_result_message(
        self,
        action: ToolAction,
        result: ToolResult,
        tool_call_id: str | None,
    ) -> Message:
        """构造工具结果消息，送回模型历史。"""

    def complete_round(self, assistant: Message, tool_result: Message) -> bool:
        """判断一组 assistant 动作与关联工具结果是否构成完整回合。"""

    def complete_round_loose(self, assistant: Message, tool_result: Message) -> bool:
        """宽松匹配完整回合；用于历史压缩对协议未知消息的兼容判定。

        默认与严格判定一致，需要放宽的协议可单独覆盖。
        """


def _tool_result_content(action: ToolAction, result: ToolResult) -> str:
    return json.dumps(
        {
            "tool_result": {
                "tool": action.tool,
                "ok": result.ok,
                "output": result.output,
            }
        },
        ensure_ascii=False,
    )


class NativeToolProtocol:
    """OpenAI 风格原生 tool calling。"""

    system_prompt = SYSTEM_PROMPT
    tools_enabled = True

    def resolve_action(self, response: ProviderResponse) -> ResolvedAction:
        if len(response.tool_calls) != 1:
            pending: list[Message] = []
            if not response.tool_calls and response.content:
                pending.append(Message("assistant", response.content))
                feedback = NATIVE_TEXT_FEEDBACK
            elif response.tool_calls:
                feedback = NATIVE_MULTIPLE_CALLS_FEEDBACK
            else:
                feedback = NATIVE_TEXT_FEEDBACK
            return ResolvedAction(
                assistant_messages=tuple(pending),
                feedback=Message("user", feedback, kind="protocol_feedback"),
                audit_error_type="ToolCallCountError",
            )
        call = response.tool_calls[0]
        return ResolvedAction(
            assistant_messages=(
                Message("assistant", response.content, tool_calls=(call,)),
            ),
            action=ToolAction(tool=call.name, arguments=call.arguments, reason=""),
            tool_call_id=call.id,
        )

    def tool_result_message(
        self,
        action: ToolAction,
        result: ToolResult,
        tool_call_id: str | None,
    ) -> Message:
        return Message(
            "tool",
            _tool_result_content(action, result),
            kind="tool_result",
            tool_call_id=tool_call_id,
        )

    def complete_round(self, assistant: Message, tool_result: Message) -> bool:
        return (
            assistant.role == "assistant"
            and tool_result.role == "tool"
            and tool_result.kind == "tool_result"
            and len(assistant.tool_calls) == 1
            and assistant.tool_calls[0].id == tool_result.tool_call_id
        )

    def complete_round_loose(self, assistant: Message, tool_result: Message) -> bool:
        return self.complete_round(assistant, tool_result)


class LegacyJsonProtocol:
    """JSON 文本动作协议。"""

    system_prompt = LEGACY_SYSTEM_PROMPT
    tools_enabled = False

    def resolve_action(self, response: ProviderResponse) -> ResolvedAction:
        raw_action = response.content or ""
        assistant_message = Message("assistant", raw_action)
        try:
            action = parse_action(raw_action)
        except ValueError as exc:
            error = str(exc)
            return ResolvedAction(
                assistant_messages=(assistant_message,),
                feedback=Message(
                    "user",
                    json.dumps(
                        {"tool_result": {"ok": False, "output": error}},
                        ensure_ascii=False,
                    ),
                    kind="tool_result",
                ),
                audit_error_type=type(exc).__name__,
            )
        return ResolvedAction(
            assistant_messages=(assistant_message,),
            action=action,
        )

    def tool_result_message(
        self,
        action: ToolAction,
        result: ToolResult,
        tool_call_id: str | None,
    ) -> Message:
        return Message(
            "user",
            _tool_result_content(action, result),
            kind="tool_result",
        )

    def complete_round(self, assistant: Message, tool_result: Message) -> bool:
        return (
            assistant.role == "assistant"
            and not assistant.tool_calls
            and tool_result.role == "user"
            and tool_result.kind == "tool_result"
        )

    def complete_round_loose(self, assistant: Message, tool_result: Message) -> bool:
        # 历史压缩可能携带 kind 未标记为 tool_result 的 user 消息，
        # 宽松判定不检查 kind，保持与原上下文压缩逻辑一致。
        return (
            assistant.role == "assistant"
            and not assistant.tool_calls
            and tool_result.role == "user"
        )


_PROTOCOLS = {
    "native": NativeToolProtocol(),
    "legacy_json": LegacyJsonProtocol(),
}
