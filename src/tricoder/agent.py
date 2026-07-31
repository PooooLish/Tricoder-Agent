"""线性消息历史驱动的 Coding Agent 循环。"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from tricoder.audit import AuditLogger
from tricoder.models import Message, RunResult, SessionContext, SessionTurnResult, ToolAction
from tricoder.policy import PolicyError
from tricoder.providers import ModelProvider, ProviderError
from tricoder.tools import ToolRegistry


SYSTEM_PROMPT = """你是一个在本地代码工作区内协作的 Coding Agent。
每轮只能返回一个 JSON 对象，不能使用 Markdown 代码块，也不能添加对象之外的文字：
{"tool":"工具名","arguments":{},"reason":"简短、可公开审计的操作理由"}

可用工具：
- list_files: {"path":"相对目录"}
- read_file: {"path":"相对文件"}
- search_text: {"path":"相对目录","query":"文本"}
- edit_file: {"path":"相对文件","old_text":"精确旧文本","new_text":"新文本"}
- create_file: {"path":"相对文件","content":"新文件完整内容"}
- run_command: {"command":"测试或静态检查命令","cwd":"可选相对目录"}
- finish: {"summary":"完成情况、验证结果和剩余风险"}

不要输出隐藏思维过程。reason 只说明当前动作的直接目的。
编辑前必须先读取目标文件；遇到工具错误时根据错误信息调整下一步。
"""


CONTEXT_COMPACTION_NOTICE = "较早的消息已被压缩，以保留最新上下文。"
AUDIT_FAILURE_MESSAGE = "无法写入审计日志，运行已安全停止"


def _message_chars(message: Message) -> int:
    """按上下文预算规则计算单条消息的字符数。"""

    return len(message.role) + len(message.content)


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
        if assistant.role == "assistant" and tool_result.role == "user":
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


def _is_complete_history_task_block(block: list[Message]) -> bool:
    """历史任务必须由 task 和至少一组完整动作/结果组成。"""

    if len(block) < 3 or (len(block) - 1) % 2 != 0:
        return False
    return all(
        block[index].role == "assistant"
        and block[index + 1].role == "user"
        and block[index + 1].kind == "tool_result"
        for index in range(1, len(block), 2)
    )


def compact_session_messages(messages: list[Message], max_chars: int) -> list[Message]:
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
        block for block in task_blocks[:-1] if _is_complete_history_task_block(block)
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
    ) -> None:
        if max_rounds <= 0:
            raise ValueError("max_rounds 必须大于 0")
        if (
            not isinstance(max_context_chars, int)
            or isinstance(max_context_chars, bool)
            or max_context_chars <= 0
        ):
            raise ValueError("max_context_chars 必须大于 0")
        self.provider = provider
        self.tools = tools
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.audit = audit
        self.observer = observer or NullObserver()

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
        messages = [Message("system", SYSTEM_PROMPT)]
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

        def current_task_has_complete_round() -> bool:
            """只在当前任务已有完整工具回合时保留其中间状态。"""

            current_task_index = max(
                index
                for index, message in enumerate(messages)
                if message.kind == "task"
            )
            return any(
                messages[index].role == "assistant"
                and messages[index + 1].kind == "tool_result"
                for index in range(current_task_index, len(messages) - 1)
            )

        def turn_result(
            result: RunResult,
            *,
            rollback_task: bool = False,
        ) -> SessionTurnResult:
            return SessionTurnResult(
                result,
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
            request_messages = compact_session_messages(messages, self.max_context_chars)
            try:
                raw_action = self.provider.complete(request_messages)
            except ProviderError as exc:
                self.observer.on_error(f"模型请求失败：{exc}")
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
                        f"模型请求失败：{exc}",
                        round_number,
                        tool_calls,
                        tuple(modified_files),
                        verification,
                    ),
                    rollback_task=True,
                )

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
            if action.tool in {"edit_file", "create_file"} and result.ok:
                # 仅使用写工具在 WorkspacePolicy 边界内确认的规范相对路径，
                # 不回读模型提供的原始参数，避免绝对路径或 dotdot 进入会话状态。
                if result.relative_path is not None and result.relative_path not in modified_files:
                    modified_files.append(result.relative_path)
                # 成功写入会使此前命令验证立即失效，必须重新验证。
                verification = "待验证"
            if action.tool == "run_command":
                verification = "通过" if result.ok else "失败"
            duration_ms = self._elapsed_ms(started)
            self.observer.on_tool_result(action, result, duration_ms)
            messages.append(
                Message(
                    "user",
                    json.dumps(
                        {
                            "tool_result": {
                                "tool": action.tool,
                                "ok": result.ok,
                                "output": result.output,
                            }
                        },
                        ensure_ascii=False,
                    ),
                    kind="tool_result",
                )
            )
            if not self._log(
                {
                    "round": round_number,
                    "status": "ok" if result.ok else "tool_error",
                    "tool": (
                        action.tool
                        if action.tool in self.tools._handlers
                        else "unknown"
                    ),
                    "reason_chars": len(action.reason),
                    "arguments": self._audit_arguments(action),
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

    def _audit_arguments(self, action: ToolAction) -> dict[str, Any]:
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
