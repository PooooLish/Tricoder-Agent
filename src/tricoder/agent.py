"""线性消息历史驱动的 Coding Agent 循环。"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Protocol

from tricoder.audit import AuditLogger
from tricoder.models import (
    Message,
    RunResult,
    SessionContext,
    SessionTurnResult,
    TokenUsage,
    ToolAction,
)
from tricoder.policy import PolicyError
from tricoder.providers import ModelProvider, ProviderError, ProviderProtocolError
from tricoder.tools import ToolRegistry


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
- search_text: {"path":"相对目录","query":"文本"}
- edit_file: {"path":"相对文件","old_text":"精确旧文本","new_text":"新文本"}
- create_file: {"path":"相对文件","content":"新文件完整内容"}
- run_command: {"command":"测试或静态检查命令","cwd":"可选相对目录"}
- finish: {"summary":"完成情况、验证结果和剩余风险"}
"""

SYSTEM_PROMPT = COMMON_SYSTEM_PROMPT + NATIVE_TOOL_PROMPT
LEGACY_SYSTEM_PROMPT = COMMON_SYSTEM_PROMPT + LEGACY_JSON_PROMPT


CONTEXT_COMPACTION_NOTICE = "较早的消息已被压缩，以保留最新上下文。"
AUDIT_FAILURE_MESSAGE = "无法写入审计日志，运行已安全停止"
NATIVE_TEXT_FEEDBACK = "本轮没有工具调用。请下一轮只选择一个可用工具调用。"
NATIVE_MULTIPLE_CALLS_FEEDBACK = "本轮包含多个工具调用，未执行任何一个。请下一轮只选择一个。"
PROTOCOL_FEEDBACK = "模型响应未满足当前协议。请下一轮按系统规则重新提交一个动作。"
PROVIDER_FAILURE_MESSAGE = "模型请求失败，运行已安全停止"


def _message_chars(message: Message) -> int:
    """按上下文预算规则计算单条消息的字符数。"""

    return message.character_budget()


def _is_complete_tool_round(
    assistant: Message,
    tool_result: Message,
    tool_protocol: str | None = None,
) -> bool:
    """按协议校验一组 assistant 动作和关联工具结果。"""

    if assistant.role != "assistant":
        return False
    if tool_protocol in {None, "native"} and tool_result.role == "tool":
        return (
            tool_result.kind == "tool_result"
            and len(assistant.tool_calls) == 1
            and assistant.tool_calls[0].id == tool_result.tool_call_id
        )
    if tool_protocol in {None, "legacy_json"} and tool_result.role == "user":
        return (
            not assistant.tool_calls
            and (
                tool_protocol is None
                or tool_result.kind == "tool_result"
            )
        )
    return False


def compact_messages(messages: list[Message], max_chars: int) -> list[Message]:
    """按完整工具交互回合压缩，并始终保留前两条固定消息。"""

    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_context_chars 必须大于 0")

    copied_messages = list(messages)
    fixed_messages = copied_messages[:2]
    later_messages = copied_messages[2:]

    rounds: list[tuple[Message, Message]] = []
    index = 0
    while index + 1 < len(later_messages):
        assistant = later_messages[index]
        tool_result = later_messages[index + 1]
        if _is_complete_tool_round(assistant, tool_result):
            rounds.append((assistant, tool_result))
            index += 2
        else:
            # 非法或孤立消息不构成可保留回合；继续寻找下一组合法边界。
            index += 1

    complete_round_history = len(rounds) * 2 == len(later_messages)
    if (
        complete_round_history
        and sum(_message_chars(message) for message in copied_messages) <= max_chars
    ):
        return copied_messages
    if not later_messages:
        return fixed_messages

    notice = Message("system", CONTEXT_COMPACTION_NOTICE)
    remaining_chars = max_chars - sum(
        _message_chars(message) for message in [*fixed_messages, notice]
    )

    retained_reversed: list[tuple[Message, Message]] = []
    for interaction in reversed(rounds):
        interaction_chars = sum(_message_chars(message) for message in interaction)
        if interaction_chars > remaining_chars:
            break
        retained_reversed.append(interaction)
        remaining_chars -= interaction_chars

    retained = [
        message
        for interaction in reversed(retained_reversed)
        for message in interaction
    ]
    return [*fixed_messages, notice, *retained]


def _normalize_history_task_block(
    block: list[Message],
    tool_protocol: str,
) -> list[Message]:
    """剔除旧任务纠错噪音，只返回任务和其中完整的动作/结果回合。"""

    if not block or block[0].kind != "task":
        return []

    retained: list[Message] = []
    index = 1
    while index + 1 < len(block):
        assistant = block[index]
        tool_result = block[index + 1]
        if _is_complete_tool_round(assistant, tool_result, tool_protocol):
            retained.extend((assistant, tool_result))
            index += 2
        else:
            index += 1
    return [block[0], *retained] if retained else []


def compact_session_messages(
    messages: list[Message],
    max_chars: int,
    tool_protocol: str,
) -> list[Message]:
    """先丢弃残缺旧任务，再按完整用户任务块压缩历史。"""

    copied_messages = list(messages)
    task_indexes = [
        index for index, message in enumerate(copied_messages) if message.kind == "task"
    ]
    if not task_indexes:
        return copied_messages

    fixed_messages = copied_messages[: task_indexes[0]]
    task_blocks = [
        copied_messages[start:end]
        for start, end in zip(task_indexes, [*task_indexes[1:], len(copied_messages)])
    ]
    latest_block = task_blocks[-1]
    complete_history_blocks = [
        normalized
        for block in task_blocks[:-1]
        if (normalized := _normalize_history_task_block(block, tool_protocol))
    ]
    normalized_messages = [
        *fixed_messages,
        *(message for block in complete_history_blocks for message in block),
        *latest_block,
    ]
    if sum(_message_chars(message) for message in normalized_messages) <= max_chars:
        return normalized_messages

    notice = Message("system", CONTEXT_COMPACTION_NOTICE)
    remaining_chars = max_chars - sum(
        _message_chars(message)
        for message in [*fixed_messages, notice, *latest_block]
    )

    retained_reversed: list[list[Message]] = []
    for block in reversed(complete_history_blocks):
        block_chars = sum(_message_chars(message) for message in block)
        if block_chars > remaining_chars:
            break
        retained_reversed.append(block)
        remaining_chars -= block_chars

    retained = [message for block in reversed(retained_reversed) for message in block]
    return [*fixed_messages, notice, *retained, *latest_block]


class AgentObserver(Protocol):
    """接收 Agent 的公开运行事件，不接触隐藏推理或凭据。"""

    def on_round_start(self, round_number: int, max_rounds: int) -> None: ...

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None: ...

    def on_action(self, action: ToolAction) -> None: ...

    def on_tool_result(
        self,
        action: ToolAction,
        result: Any,
        duration_ms: int,
    ) -> None: ...

    def on_error(self, message: str) -> None: ...


class NullObserver:
    """在库调用或测试中保持完全静默的默认观察者。"""

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        return None

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
        return None

    def on_action(self, action: ToolAction) -> None:
        return None

    def on_tool_result(self, action: ToolAction, result: Any, duration_ms: int) -> None:
        return None

    def on_error(self, message: str) -> None:
        return None


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


class CodingAgent:
    """在最大轮数限制内请求动作、执行工具并回填结果。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        max_rounds: int = 12,
        max_context_chars: int = 80_000,
        audit: AuditLogger | None = None,
        observer: AgentObserver | None = None,
        tool_protocol: str = "native",
    ) -> None:
        if max_rounds <= 0:
            raise ValueError("max_rounds 必须大于 0")
        if (
            not isinstance(max_context_chars, int)
            or isinstance(max_context_chars, bool)
            or max_context_chars <= 0
        ):
            raise ValueError("max_context_chars 必须大于 0")
        if tool_protocol not in {"native", "legacy_json"}:
            raise ValueError("tool_protocol 必须是 native 或 legacy_json")
        self.provider = provider
        self.tools = tools
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.audit = audit
        self.observer = observer or NullObserver()
        self.tool_protocol = tool_protocol

    def run(self, task: str) -> RunResult:
        """保持一次性运行接口的返回类型不变。"""

        return self.run_with_context(task, SessionContext()).result

    def run_with_context(
        self,
        task: str,
        context: SessionContext,
    ) -> SessionTurnResult:
        """在不可变会话上下文上执行一个任务，并返回新的快照。"""

        if not task.strip():
            return SessionTurnResult(RunResult(False, "任务描述不能为空", 0), context)
        system_prompt = (
            SYSTEM_PROMPT
            if self.tool_protocol == "native"
            else LEGACY_SYSTEM_PROMPT
        )
        messages = [Message("system", system_prompt)]
        if context.persisted_summary:
            messages.append(
                Message(
                    "user",
                    f"持久化会话摘要：{context.persisted_summary}",
                    kind="persisted_summary",
                )
            )
        history_start = len(messages)
        messages.extend(context.messages)
        messages.append(Message("user", f"用户任务：{task.strip()}", kind="task"))
        tool_calls = 0
        modified_files = list(context.modified_files)
        verification = context.verification
        accumulated_usage: TokenUsage | None = None

        def current_task_has_complete_round() -> bool:
            """只在当前任务已有完整工具回合时保留其中间状态。"""

            current_task_index = max(
                index
                for index, message in enumerate(messages)
                if message.kind == "task"
            )
            return any(
                _is_complete_tool_round(
                    messages[index],
                    messages[index + 1],
                    self.tool_protocol,
                )
                for index in range(current_task_index, len(messages) - 1)
            )

        def turn_result(
            result: RunResult,
            *,
            rollback_task: bool = False,
        ) -> SessionTurnResult:
            return SessionTurnResult(
                replace(result, usage=accumulated_usage),
                SessionContext(
                    messages=(
                        context.messages
                        if rollback_task and not current_task_has_complete_round()
                        else tuple(messages[history_start:])
                    ),
                    persisted_summary=context.persisted_summary,
                    modified_files=tuple(modified_files),
                    verification=verification,
                ),
            )

        if self.audit is not None:
            try:
                self.audit.prepare()
            except OSError:
                self.observer.on_error(AUDIT_FAILURE_MESSAGE)
                return turn_result(
                    RunResult(False, AUDIT_FAILURE_MESSAGE, 0), rollback_task=True
                )

        for round_number in range(1, self.max_rounds + 1):
            self.observer.on_round_start(round_number, self.max_rounds)
            started = time.perf_counter()
            request_messages = compact_session_messages(
                messages,
                self.max_context_chars,
                self.tool_protocol,
            )
            provider_tools = (
                self.tools.definitions if self.tool_protocol == "native" else ()
            )
            try:
                response = self.provider.complete(request_messages, provider_tools)
            except ProviderProtocolError as exc:
                self.observer.on_error(PROTOCOL_FEEDBACK)
                if not self._log(
                    {
                        "round": round_number,
                        "status": "provider_protocol_error",
                        "error_type": type(exc).__name__,
                        "error_chars": len(str(exc)),
                        "duration_ms": self._elapsed_ms(started),
                    }
                ):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )
                messages.append(
                    Message("user", PROTOCOL_FEEDBACK, kind="protocol_feedback")
                )
                continue
            except ProviderError as exc:
                self.observer.on_error(PROVIDER_FAILURE_MESSAGE)
                if not self._log(
                    {
                        "round": round_number,
                        "status": "provider_error",
                        "error_type": type(exc).__name__,
                        "error_chars": len(str(exc)),
                    }
                ):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )
                return turn_result(
                    RunResult(
                        False,
                        PROVIDER_FAILURE_MESSAGE,
                        round_number,
                        tool_calls,
                        tuple(modified_files),
                        verification,
                    ),
                    rollback_task=True,
                )

            if response.usage is not None:
                accumulated_usage = (
                    response.usage
                    if accumulated_usage is None
                    else accumulated_usage.merge(response.usage)
                )
                on_provider_usage = getattr(self.observer, "on_provider_usage", None)
                if callable(on_provider_usage):
                    on_provider_usage(round_number, response.usage)
                if not self._audit_usage(round_number, response.usage):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        ),
                        rollback_task=True,
                    )

            tool_call_id: str | None = None
            if self.tool_protocol == "native":
                if len(response.tool_calls) != 1:
                    pending_feedback: list[Message] = []
                    if not response.tool_calls and response.content:
                        pending_feedback.append(
                            Message("assistant", response.content)
                        )
                        feedback = NATIVE_TEXT_FEEDBACK
                    elif response.tool_calls:
                        feedback = NATIVE_MULTIPLE_CALLS_FEEDBACK
                    else:
                        feedback = NATIVE_TEXT_FEEDBACK
                    self.observer.on_error(feedback)
                    pending_feedback.append(
                        Message("user", feedback, kind="protocol_feedback")
                    )
                    if not self._log(
                        {
                            "round": round_number,
                            "status": "invalid_action",
                            "error_type": "ToolCallCountError",
                            "error_chars": len(feedback),
                            "duration_ms": self._elapsed_ms(started),
                        }
                    ):
                        return turn_result(
                            self._audit_failure_result(
                                round_number,
                                tool_calls,
                                modified_files,
                                verification,
                            ),
                            rollback_task=True,
                        )
                    messages.extend(pending_feedback)
                    continue

                call = response.tool_calls[0]
                messages.append(
                    Message(
                        "assistant",
                        response.content,
                        tool_calls=(call,),
                    )
                )
                definition = self.tools.describe(call.name)
                reason = (
                    definition.description
                    if definition is not None
                    else "请求执行未注册的工具。"
                )
                action = ToolAction(
                    tool=call.name,
                    arguments=call.arguments,
                    reason=reason,
                )
                tool_call_id = call.id
            else:
                raw_action = response.content or ""
                messages.append(Message("assistant", raw_action))
                try:
                    action = parse_action(raw_action)
                except ValueError as exc:
                    error = str(exc)
                    self.observer.on_error(error)
                    messages.append(
                        Message(
                            "user",
                            json.dumps(
                                {"tool_result": {"ok": False, "output": error}},
                                ensure_ascii=False,
                            ),
                            kind="tool_result",
                        )
                    )
                    if not self._log(
                        {
                            "round": round_number,
                            "status": "invalid_action",
                            "error_type": type(exc).__name__,
                            "error_chars": len(error),
                            "duration_ms": self._elapsed_ms(started),
                        }
                    ):
                        return turn_result(
                            self._audit_failure_result(
                                round_number,
                                tool_calls,
                                modified_files,
                                verification,
                            )
                        )
                    continue

            self.observer.on_action(action)
            tool_calls += 1
            result = self.tools.execute(action.tool, action.arguments)
            # 只消费工具在文件安全边界内确认的规范路径，不回读模型原始参数。
            changed_paths = tuple(
                dict.fromkeys(
                    ([result.relative_path] if result.relative_path is not None else [])
                    + list(result.modified_paths)
                )
            ) if result.ok else ()
            for changed_path in changed_paths:
                if changed_path not in modified_files:
                    modified_files.append(changed_path)
            if changed_paths:
                # 成功写入会使此前命令验证立即失效，必须重新验证。
                verification = "待验证"
            if action.tool == "run_command":
                verification = "通过" if result.ok else "失败"
            duration_ms = self._elapsed_ms(started)
            self.observer.on_tool_result(action, result, duration_ms)
            tool_result_content = json.dumps(
                {
                    "tool_result": {
                        "tool": action.tool,
                        "ok": result.ok,
                        "output": result.output,
                    }
                },
                ensure_ascii=False,
            )
            if tool_call_id is not None:
                messages.append(
                    Message(
                        "tool",
                        tool_result_content,
                        kind="tool_result",
                        tool_call_id=tool_call_id,
                    )
                )
            else:
                messages.append(
                    Message(
                        "user",
                        tool_result_content,
                        kind="tool_result",
                    )
                )
            if not self._log(
                {
                    "round": round_number,
                    "status": "ok" if result.ok else "tool_error",
                    "tool": (
                        action.tool
                        if self.tools.contains(action.tool)
                        else "unknown"
                    ),
                    "reason_chars": len(action.reason),
                    "arguments": self._audit_arguments(action, result),
                    "output_chars": len(result.output),
                    "duration_ms": duration_ms,
                }
            ):
                return turn_result(
                    self._audit_failure_result(
                        round_number,
                        tool_calls,
                        modified_files,
                        verification,
                    )
                )
            if action.tool == "finish":
                completed = result.ok and (not modified_files or verification == "通过")
                summary = result.output
                if result.ok and modified_files and verification == "待验证":
                    summary = f"{summary}；文件修改后尚未运行验证命令"
                elif result.ok and modified_files and verification == "失败":
                    summary = f"{summary}；文件修改后的验证失败"
                return turn_result(
                    RunResult(
                        completed,
                        summary,
                        round_number,
                        tool_calls,
                        tuple(modified_files),
                        verification,
                    )
                )
        summary = f"达到最大轮数 {self.max_rounds}，任务已安全停止"
        self.observer.on_error(summary)
        return turn_result(
            RunResult(
                False,
                summary,
                self.max_rounds,
                tool_calls,
                tuple(modified_files),
                verification,
            )
        )

    def _log(self, event: dict[str, Any]) -> bool:
        if self.audit is None:
            return True
        try:
            self.audit.log(event)
        except OSError:
            self.observer.on_error(AUDIT_FAILURE_MESSAGE)
            return False
        return True

    def _audit_usage(self, round_number: int, usage: TokenUsage) -> bool:
        event: dict[str, Any] = {"round": round_number, "status": "provider_usage"}
        for field, value in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cached", usage.cached_tokens),
            ("cache_miss", usage.cache_miss_tokens),
        ):
            if value is not None:
                event[field] = value
        return self._log(event)

    @staticmethod
    def _audit_failure_result(
        round_number: int,
        tool_calls: int,
        modified_files: list[str],
        verification: str,
    ) -> RunResult:
        return RunResult(
            False,
            AUDIT_FAILURE_MESSAGE,
            round_number,
            tool_calls,
            tuple(modified_files),
            verification,
        )

    def _audit_arguments(
        self,
        action: ToolAction,
        result: ToolResult,
    ) -> dict[str, Any]:
        """只保留审计所需元数据，避免重复保存源码和任务摘要。"""

        arguments = action.arguments
        if action.tool in {"list_files", "read_file"}:
            return {"path": arguments.get("path", ".")}
        if action.tool == "search_text":
            query = arguments.get("query", "")
            return {
                "path": arguments.get("path", "."),
                "query_chars": len(query) if isinstance(query, str) else 0,
            }
        if action.tool == "edit_file":
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            return {
                "path": arguments.get("path"),
                "old_text_chars": len(old_text) if isinstance(old_text, str) else 0,
                "new_text_chars": len(new_text) if isinstance(new_text, str) else 0,
            }
        if action.tool == "create_file":
            content = arguments.get("content", "")
            return {
                "path": arguments.get("path"),
                "content_chars": len(content) if isinstance(content, str) else 0,
            }
        if action.tool == "apply_patch":
            patch_text = arguments.get("patch", "")
            return {
                "patch_chars": len(patch_text) if isinstance(patch_text, str) else 0,
                "paths": result.audit_paths,
                "file_count": len(result.audit_paths),
                "change_chars": result.change_chars,
            }
        if action.tool == "run_command":
            command = arguments.get("command", "")
            metadata = self.tools.context.command_policy.audit_metadata(
                command if isinstance(command, str) else "",
            )
            cwd = arguments.get("cwd", ".")
            if isinstance(cwd, str):
                try:
                    resolved_cwd = self.tools.context.workspace_policy.resolve_path(cwd)
                    relative_cwd = resolved_cwd.relative_to(
                        self.tools.context.workspace_policy.workspace
                    )
                    metadata["cwd"] = {
                        "is_workspace": not relative_cwd.parts,
                        "depth": len(relative_cwd.parts),
                    }
                except (PolicyError, ValueError):
                    metadata["cwd"] = {
                        "valid": False,
                        "chars": len(cwd),
                    }
            else:
                metadata["cwd"] = {"valid": False, "chars": 0}
            return metadata
        if action.tool == "finish":
            summary = arguments.get("summary", "")
            return {"summary_chars": len(summary) if isinstance(summary, str) else 0}
        return {"argument_count": len(arguments)}

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.perf_counter() - started) * 1000)
