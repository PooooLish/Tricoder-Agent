"""线性消息历史驱动的 Coding Agent 循环。"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import replace
from typing import Any, Protocol, runtime_checkable

from tricoder.audit import AuditLogger
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import (
    AgentEvent,
    ApprovalRequested,
    ContextCompacted,
    EventSink,
    PlanningCompleted,
    ProviderCompleted,
    RoundStarted,
    RuntimeCompleted,
    RuntimeFailed,
    TextDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    UsageReported,
)
from tricoder.context import (
    CONTEXT_COMPACTION_NOTICE,
    ContextBudget,
    ContextManager,
)
from tricoder.extensions.models import ToolOrigin
from tricoder.models import (
    Message,
    ProviderResponse,
    RunResult,
    SessionContext,
    SessionTurnResult,
    TokenUsage,
    ToolAction,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tricoder.policy import PolicyError
from tricoder.providers import ModelProvider, ProviderError, ProviderProtocolError
from tricoder.protocols import (
    COMMON_SYSTEM_PROMPT,
    LEGACY_JSON_PROMPT,
    LEGACY_SYSTEM_PROMPT,
    NATIVE_TEXT_FEEDBACK,
    PROTOCOL_FEEDBACK,
    SYSTEM_PROMPT,
    ActionProtocol,
    _PROTOCOLS,
    parse_action,
)
from tricoder.tools import ToolRegistry


AUDIT_FAILURE_MESSAGE = "无法写入审计日志，运行已安全停止"
PROVIDER_FAILURE_MESSAGE = "模型请求失败，运行已安全停止"
PLANNING_PROMPT = """在开始执行前，请先输出一份简短的分步执行计划。
只输出计划，不要调用工具，不要添加任何额外说明。
计划格式为 JSON：
{"steps": ["第 1 步...", "第 2 步...", "第 3 步..."]}
步骤必须具体、可执行，数量控制在 3 到 8 步。"""

# 规划阶段审计失败时的终止哨兵。
_PLAN_ABORT = object()


def _is_complete_tool_round(
    assistant: Message,
    tool_result: Message,
    tool_protocol: str | None = None,
) -> bool:
    """按协议校验一组 assistant 动作和关联工具结果。

    ``tool_protocol`` 为 None 时按各协议的宽松判定匹配，供历史压缩逻辑使用；
    指定协议时使用该协议的严格判定。新增协议无需再修改本函数。
    """

    if assistant.role != "assistant":
        return False
    if tool_protocol is None:
        return any(
            protocol.complete_round_loose(assistant, tool_result)
            for protocol in _PROTOCOLS.values()
        )
    return _PROTOCOLS[tool_protocol].complete_round(assistant, tool_result)


def _complete_round_tail(
    messages: list[Message],
    index: int,
    tool_protocol: str | None = None,
) -> int | None:
    """若 ``messages[index:]`` 以完整工具回合开头，返回回合结束后的下标；否则 None。

    native：一个 assistant 携带 N 个 ``tool_calls``，后跟 N 个按 id 匹配的
    ``tool`` 结果；legacy：assistant（无 tool_calls）+ 一个 user 结果。
    ``tool_protocol`` 为 None 时按宽松语义判定（legacy 不检查 kind）。
    """

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
    """字符预算兼容代理；具体回合压缩由 ``ContextManager`` 负责。"""

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
    """字符预算兼容代理；保持旧调用点和保留顺序。"""

    manager = ContextManager(
        ContextBudget(max_chars=max_chars),
        _PROTOCOLS[tool_protocol],
    )
    return list(manager.prepare(messages).messages)


@runtime_checkable
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


@runtime_checkable
class ProviderUsageObserver(Protocol):
    """可选接收逐轮归一化 Provider 用量。"""

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None: ...


def _notify_provider_usage(
    observer: AgentObserver,
    round_number: int,
    usage: TokenUsage,
) -> None:
    if isinstance(observer, ProviderUsageObserver):
        observer.on_provider_usage(round_number, usage)


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


class CodingAgent:
    """在最大轮数限制内请求动作、执行工具并回填结果。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        max_rounds: int = 30,
        max_context_chars: int = 80_000,
        audit: AuditLogger | None = None,
        observer: AgentObserver | None = None,
        tool_protocol: str = "native",
        plan_enabled: bool = True,
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
        if not isinstance(plan_enabled, bool):
            raise ValueError("plan_enabled 必须是布尔值")
        self.provider = provider
        self.tools = tools
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.audit = audit
        self.observer = observer or NullObserver()
        self.tool_protocol = tool_protocol
        self.plan_enabled = plan_enabled
        self._protocol: ActionProtocol = _PROTOCOLS[tool_protocol]
        # 旧配置仍以字符数命名，因此同时保留字符硬上限；同值 token 上限
        # 让 Provider usage 可在字符估算明显偏低时触发完整回合压缩。
        self.context_manager = ContextManager(
            ContextBudget(
                max_tokens=max_context_chars,
                max_chars=max_context_chars,
            ),
            self._protocol,
        )

    def run(
        self,
        task: str,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> RunResult:
        """保持一次性运行接口的返回类型不变。"""

        return self.run_with_context(
            task,
            SessionContext(),
            cancellation=cancellation,
            event_sink=event_sink,
        ).result

    def run_with_context(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> SessionTurnResult:
        """同步兼容入口；已有事件循环中必须调用异步接口。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_with_context_async(
                    task,
                    context,
                    cancellation=cancellation,
                    event_sink=event_sink,
                )
            )
        raise RuntimeError("同步 Agent 入口不能在已运行的事件循环中调用")

    async def run_with_context_async(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> SessionTurnResult:
        """在不可变会话上下文上异步执行任务，并发布类型化事件。"""

        cancellation = cancellation or CancellationToken()

        if not task.strip():
            result = RunResult(False, "任务描述不能为空", 0)
            self._emit(event_sink, RuntimeFailed("input", result.summary))
            return SessionTurnResult(result, context)
        system_prompt = self._protocol.system_prompt
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
                _complete_round_tail(messages, index, self.tool_protocol) is not None
                for index in range(current_task_index, len(messages) - 1)
            )

        def turn_result(
            result: RunResult,
            *,
            rollback_task: bool = False,
        ) -> SessionTurnResult:
            turn = SessionTurnResult(
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
            if result.ok:
                self._emit(event_sink, RuntimeCompleted(turn.result))
            else:
                category = "cancelled" if result.summary == "任务已取消" else "runtime"
                self._emit(event_sink, RuntimeFailed(category, result.summary))
            return turn

        if self.audit is not None:
            try:
                self.audit.prepare()
            except OSError:
                self.observer.on_error(AUDIT_FAILURE_MESSAGE)
                return turn_result(
                    RunResult(False, AUDIT_FAILURE_MESSAGE, 0), rollback_task=True
                )

        if self.plan_enabled:
            try:
                plan_usage = await self._planning_round_async(
                    messages,
                    tool_calls,
                    modified_files,
                    verification,
                    cancellation,
                    event_sink,
                )
            except CancellationError:
                return turn_result(
                    RunResult(
                        False,
                        "任务已取消",
                        0,
                        tool_calls,
                        tuple(modified_files),
                        verification,
                    ),
                    rollback_task=True,
                )
            if plan_usage is _PLAN_ABORT:
                return turn_result(
                    self._audit_failure_result(
                        0, tool_calls, modified_files, verification
                    ),
                    rollback_task=True,
                )
            if plan_usage is not None:
                accumulated_usage = (
                    plan_usage
                    if accumulated_usage is None
                    else accumulated_usage.merge(plan_usage)
                )

        for round_number in range(1, self.max_rounds + 1):
            if cancellation.is_cancelled:
                return turn_result(
                    RunResult(False, "任务已取消", round_number - 1, tool_calls, tuple(modified_files), verification),
                    rollback_task=True,
                )
            self.observer.on_round_start(round_number, self.max_rounds)
            self._emit(event_sink, RoundStarted(round_number, self.max_rounds))
            started = time.perf_counter()
            context_snapshot = self.context_manager.prepare(messages)
            request_messages = list(context_snapshot.messages)
            if context_snapshot.compacted:
                self._emit(
                    event_sink,
                    ContextCompacted(
                        before_chars=sum(message.character_budget() for message in messages),
                        after_chars=context_snapshot.character_count,
                    ),
                )
            provider_tools = (
                self.tools.definitions if self._protocol.tools_enabled else ()
            )
            try:
                response = await self._request_provider_async(
                    request_messages,
                    provider_tools,
                    cancellation,
                    event_sink,
                )
            except CancellationError:
                return turn_result(
                    RunResult(False, "任务已取消", round_number, tool_calls, tuple(modified_files), verification),
                    rollback_task=True,
                )
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
                _notify_provider_usage(self.observer, round_number, response.usage)
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

            resolved = self._protocol.resolve_action(response)
            messages.extend(resolved.assistant_messages)
            if response.usage is not None:
                self.context_manager.record_usage(
                    response.usage,
                    [*request_messages, *resolved.assistant_messages],
                )
            if not resolved.actions:
                feedback = resolved.feedback or Message(
                    "user", PROTOCOL_FEEDBACK, kind="protocol_feedback"
                )
                self.observer.on_error(feedback.content or "")
                messages.append(feedback)
                if not self._log(
                    {
                        "round": round_number,
                        "status": "invalid_action",
                        "error_type": resolved.audit_error_type or "ActionProtocolError",
                        "error_chars": len(feedback.content or ""),
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
                continue

            # 一次可执行多个动作：逐个顺序执行、独立审批与审计，最后统一回填。
            for action_index, (action, tool_call_id) in enumerate(
                zip(resolved.actions, resolved.tool_call_ids)
            ):
                if cancellation.is_cancelled:
                    for later_action, later_id in zip(
                        resolved.actions[action_index:],
                        resolved.tool_call_ids[action_index:],
                    ):
                        messages.append(
                            self._protocol.tool_result_message(
                                later_action,
                                ToolResult(False, "任务已取消，该动作未执行"),
                                later_id,
                            )
                        )
                    return turn_result(
                        RunResult(False, "任务已取消", round_number, tool_calls, tuple(modified_files), verification)
                    )
                if not action.reason:
                    definition = self.tools.describe(action.tool)
                    action = replace(
                        action,
                        reason=(
                            definition.description
                            if definition is not None
                            else "请求执行未注册的工具。"
                        ),
                    )
                self.observer.on_action(action)
                requires_approval = getattr(self.tools, "requires_approval", None)
                if callable(requires_approval) and requires_approval(action.tool):
                    self._emit(event_sink, ApprovalRequested(
                        ToolCall(tool_call_id or f"legacy-{round_number}-{action_index}", action.tool, action.arguments),
                        action.reason,
                    ))
                event_call = ToolCall(
                    tool_call_id or f"legacy-{round_number}-{action_index}",
                    action.tool,
                    action.arguments,
                )
                self._emit(event_sink, ToolExecutionStarted(event_call))
                tool_calls += 1
                action_started = time.perf_counter()
                try:
                    execute_async = getattr(self.tools, "execute_async", None)
                    if callable(execute_async):
                        execute_parameters = inspect.signature(execute_async).parameters
                        execute_kwargs: dict[str, Any] = {"cancellation": cancellation}
                        if "call_id" in execute_parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in execute_parameters.values()
                        ):
                            execute_kwargs["call_id"] = event_call.id
                        result = await execute_async(
                            action.tool,
                            action.arguments,
                            **execute_kwargs,
                        )
                    else:
                        cancellation.raise_if_cancelled()
                        execute_parameters = inspect.signature(self.tools.execute).parameters
                        execute_kwargs = {}
                        if "call_id" in execute_parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in execute_parameters.values()
                        ):
                            execute_kwargs["call_id"] = event_call.id
                        result = await asyncio.to_thread(
                            self.tools.execute,
                            action.tool,
                            action.arguments,
                            **execute_kwargs,
                        )
                except CancellationError:
                    result = ToolResult(False, "任务已取消，该动作未完成")
                    messages.append(
                        self._protocol.tool_result_message(action, result, tool_call_id)
                    )
                    for later_action, later_id in zip(
                        resolved.actions[action_index + 1 :],
                        resolved.tool_call_ids[action_index + 1 :],
                    ):
                        messages.append(
                            self._protocol.tool_result_message(
                                later_action,
                                ToolResult(False, "任务已取消，该动作未执行"),
                                later_id,
                            )
                        )
                    self._emit(event_sink, ToolExecutionCompleted(event_call.id, result))
                    return turn_result(
                        RunResult(False, "任务已取消", round_number, tool_calls, tuple(modified_files), verification)
                    )
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
                if (
                    action.tool == "run_command"
                    and result.verification_passed is not None
                ):
                    if not result.verification_passed:
                        verification = "失败"
                    elif verification != "失败":
                        # 同一修改版本内失败保持有效；新的文件修改会先重置为待验证。
                        verification = "通过"
                duration_ms = self._elapsed_ms(action_started)
                self.observer.on_tool_result(action, result, duration_ms)
                self._emit(event_sink, ToolExecutionCompleted(event_call.id, result))
                messages.append(
                    self._protocol.tool_result_message(action, result, tool_call_id)
                )
                tool_event: dict[str, Any] = {
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
                origin_resolver = getattr(self.tools, "origin", None)
                if callable(origin_resolver) and self.tools.contains(action.tool):
                    try:
                        origin = origin_resolver(action.tool)
                    except (TypeError, ValueError):
                        origin = None
                    if isinstance(origin, ToolOrigin):
                        tool_event["origin"] = {
                            "kind": origin.kind,
                            "id": origin.id,
                            "risk": origin.risk,
                        }
                if result.spill_reference is not None:
                    tool_event["spill"] = {
                        "reference": result.spill_reference,
                        "bytes": result.spill_bytes,
                        "sha256": result.spill_sha256,
                    }
                if not self._log(tool_event):
                    return turn_result(
                        self._audit_failure_result(
                            round_number,
                            tool_calls,
                            modified_files,
                            verification,
                        )
                    )
                if action.tool == "finish":
                    # finish 后若仍有模型声明的后续工具调用，必须为每个 tool_call
                    # 回填“未执行”结果，保证 Provider 历史始终包含完整 tool-result。
                    for later_action, later_id in zip(
                        resolved.actions[action_index + 1 :],
                        resolved.tool_call_ids[action_index + 1 :],
                    ):
                        messages.append(
                            self._protocol.tool_result_message(
                                later_action,
                                ToolResult(False, "任务已结束，该动作未执行"),
                                later_id,
                            )
                        )
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

    async def _planning_round_async(
        self,
        messages: list[Message],
        tool_calls: int,
        modified_files: list[str],
        verification: str,
        cancellation: CancellationToken,
        event_sink: EventSink | None,
    ) -> object:
        """任务执行前的规划阶段（round 0）：生成并注入分步计划。

        返回该次请求的 TokenUsage 供外层累计；审计失败返回 _PLAN_ABORT；
        计划失败时降级为无计划执行并返回 None。
        """

        started = time.perf_counter()
        try:
            response = await self._request_provider_async(
                [*messages, Message("user", PLANNING_PROMPT)],
                (),
                cancellation,
                event_sink,
            )
        except CancellationError:
            raise
        except ProviderError as exc:
            self.observer.on_error("规划失败，将直接执行")
            if not self._log(
                {
                    "round": 0,
                    "status": "plan_failed",
                    "error_type": type(exc).__name__,
                    "error_chars": len(str(exc)),
                    "duration_ms": self._elapsed_ms(started),
                }
            ):
                return _PLAN_ABORT
            return None

        if response.usage is not None:
            _notify_provider_usage(self.observer, 0, response.usage)
            if not self._audit_usage(0, response.usage):
                return _PLAN_ABORT

        raw = response.content or ""
        plan = self._parse_plan(raw)
        if plan is not None:
            messages.append(Message("system", f"执行计划：\n{plan}"))
            self._emit(event_sink, PlanningCompleted(plan))
            if not self._log(
                {
                    "round": 0,
                    "status": "plan",
                    "plan_chars": len(plan),
                    "plan_steps": plan.count("\n") + 1,
                    "duration_ms": self._elapsed_ms(started),
                }
            ):
                return _PLAN_ABORT
        else:
            if not self._log(
                {
                    "round": 0,
                    "status": "plan_failed",
                    "error_type": "PlanParseError",
                    "plan_chars": len(raw),
                    "duration_ms": self._elapsed_ms(started),
                }
            ):
                return _PLAN_ABORT
        return response.usage

    async def _request_provider_async(
        self,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
        cancellation: CancellationToken,
        event_sink: EventSink | None,
    ) -> ProviderResponse:
        """消费流式 Provider；旧 fake/实现通过 complete 保持兼容。"""

        stream = getattr(self.provider, "stream", None)
        if not callable(stream):
            cancellation.raise_if_cancelled()
            response = await asyncio.to_thread(self.provider.complete, messages, tools)
            for event in self._response_events(response):
                self._emit(event_sink, event)
            return response

        content_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        async for event in stream(messages, tools, cancellation=cancellation):
            cancellation.raise_if_cancelled()
            self._emit(event_sink, event)
            if isinstance(event, TextDelta):
                content_parts.append(event.text)
            elif isinstance(event, ToolCallCompleted):
                calls.append(event.call)
            elif isinstance(event, UsageReported):
                usage = event.usage if usage is None else usage.merge(event.usage)
            elif isinstance(event, ProviderCompleted):
                finish_reason = event.finish_reason
                completed = True
        if not completed:
            raise ProviderProtocolError("模型服务流缺少完成事件")
        content = "".join(content_parts) or None
        if content is None and not calls:
            raise ProviderProtocolError("模型服务流没有可用响应")
        return ProviderResponse(content, tuple(calls), finish_reason, usage)

    @staticmethod
    def _response_events(response: ProviderResponse) -> tuple[AgentEvent, ...]:
        """把仅支持 complete 的旧实现转换成等价类型化事件。"""

        events: list[AgentEvent] = []
        if response.content:
            events.append(TextDelta(response.content))
        events.extend(ToolCallCompleted(call) for call in response.tool_calls)
        if response.usage is not None:
            events.append(UsageReported(response.usage))
        events.append(ProviderCompleted(response.finish_reason))
        return tuple(events)

    @staticmethod
    def _emit(event_sink: EventSink | None, event: AgentEvent) -> None:
        if event_sink is not None:
            event_sink(event)

    @staticmethod
    def _parse_plan(raw: str) -> str | None:
        """宽容解析模型计划：JSON steps > Markdown 列表 > 原文降级。"""
        text = raw.strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                steps = decoded.get("steps")
                if isinstance(steps, list):
                    clean = [
                        str(step).strip() for step in steps if str(step).strip()
                    ]
                    if clean:
                        return "\n".join(
                            f"{index + 1}. {step}"
                            for index, step in enumerate(clean)
                        )
        except (json.JSONDecodeError, TypeError):
            pass
        lines = [
            line.strip().lstrip("-*").strip().lstrip("0123456789. ").strip()
            for line in text.splitlines()
            if line.strip()
        ]
        if len(lines) >= 2:
            return "\n".join(f"{index + 1}. {line}" for index, line in enumerate(lines))
        return text

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
        if action.tool == "read_tool_result":
            return {
                "reference": arguments.get("reference"),
                "offset": arguments.get("offset", 0),
            }
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
