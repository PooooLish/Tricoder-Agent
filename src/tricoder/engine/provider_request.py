"""Provider 响应收集和任务规划阶段。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import (
    AgentEvent,
    EventSink,
    PlanningCompleted,
    ProviderCompleted,
    TextDelta,
    ToolCallCompleted,
    UsageReported,
)
from tricoder.engine.telemetry import AgentObserver, elapsed_ms, emit, notify_provider_usage
from tricoder.models import Message, ProviderResponse, TokenUsage, ToolCall, ToolDefinition
from tricoder.providers import ModelProvider, ProviderError, ProviderProtocolError
from tricoder.task_cleanup import run_in_cleanup_thread


PLANNING_PROMPT = """在开始执行前，请先输出一份简短的分步执行计划。
只输出计划，不要调用工具，不要添加任何额外说明。
计划格式为 JSON：
{"steps": ["第 1 步...", "第 2 步...", "第 3 步..."]}
步骤必须具体、可执行，数量控制在 3 到 8 步。"""


def response_events(response: ProviderResponse) -> tuple[AgentEvent, ...]:
    """把仅支持 ``complete`` 的 Provider 响应转换为类型化事件。"""

    events: list[AgentEvent] = []
    if response.content:
        events.append(TextDelta(response.content))
    events.extend(ToolCallCompleted(call) for call in response.tool_calls)
    if response.usage is not None:
        events.append(UsageReported(response.usage))
    events.append(ProviderCompleted(response.finish_reason))
    return tuple(events)


class ProviderResponseCollector:
    """消费流式或兼容 ``complete`` 的 Provider，并统一返回完整响应。"""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def request(
        self,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
        cancellation: CancellationToken,
        event_sink: EventSink | None,
    ) -> ProviderResponse:
        cancellation.raise_if_cancelled()
        stream = getattr(self.provider, "stream", None)
        if not callable(stream):
            response = await run_in_cleanup_thread(
                self.provider.complete,
                messages,
                tools,
            )
            for event in response_events(response):
                emit(event_sink, event)
            return response

        content_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        async for event in stream(messages, tools, cancellation=cancellation):
            cancellation.raise_if_cancelled()
            emit(event_sink, event)
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


def parse_plan(raw: str) -> str | None:
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
                clean = [str(step).strip() for step in steps if str(step).strip()]
                if clean:
                    return "\n".join(
                        f"{index + 1}. {step}" for index, step in enumerate(clean)
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


@dataclass(frozen=True)
class PlanningOutcome:
    """规划阶段对主循环可见的最小结果。"""

    usage: TokenUsage | None = None
    audit_failed: bool = False


ProviderRequest = Callable[
    [list[Message], tuple[ToolDefinition, ...], CancellationToken, EventSink | None],
    Awaitable[ProviderResponse],
]
AuditEvent = Callable[[dict[str, object]], bool]
AuditUsage = Callable[[int, TokenUsage], bool]


class PlanningPhase:
    """执行 round 0 规划，不持有任务状态或执行工具。"""

    def __init__(
        self,
        request: ProviderRequest,
        observer: AgentObserver,
        log: AuditEvent,
        audit_usage: AuditUsage,
    ) -> None:
        self._request = request
        self._observer = observer
        self._log = log
        self._audit_usage = audit_usage

    async def run(
        self,
        messages: list[Message],
        cancellation: CancellationToken,
        event_sink: EventSink | None,
        *,
        request_messages: list[Message] | None = None,
    ) -> PlanningOutcome:
        started = time.perf_counter()
        try:
            response = await self._request(
                [
                    *(request_messages if request_messages is not None else messages),
                    Message("user", PLANNING_PROMPT),
                ],
                (),
                cancellation,
                event_sink,
            )
        except CancellationError:
            raise
        except ProviderError as exc:
            self._observer.on_error("规划失败，将直接执行")
            logged = self._log(
                {
                    "round": 0,
                    "status": "plan_failed",
                    "error_type": type(exc).__name__,
                    "error_chars": len(str(exc)),
                    "duration_ms": elapsed_ms(started),
                }
            )
            return PlanningOutcome(audit_failed=not logged)

        if response.usage is not None:
            notify_provider_usage(self._observer, 0, response.usage)
            if not self._audit_usage(0, response.usage):
                return PlanningOutcome(audit_failed=True)

        raw = response.content or ""
        plan = parse_plan(raw)
        if plan is not None:
            messages.append(Message("system", f"执行计划：\n{plan}"))
            emit(event_sink, PlanningCompleted(plan))
            logged = self._log(
                {
                    "round": 0,
                    "status": "plan",
                    "plan_chars": len(plan),
                    "plan_steps": plan.count("\n") + 1,
                    "duration_ms": elapsed_ms(started),
                }
            )
        else:
            logged = self._log(
                {
                    "round": 0,
                    "status": "plan_failed",
                    "error_type": "PlanParseError",
                    "plan_chars": len(raw),
                    "duration_ms": elapsed_ms(started),
                }
            )
        return PlanningOutcome(response.usage, audit_failed=not logged)
