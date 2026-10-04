"""TriCoder CodingAgent 公开门面与历史兼容导出。"""

from __future__ import annotations

import asyncio
from typing import Any

from tricoder.audit import AuditLogger
from tricoder.context import CONTEXT_COMPACTION_NOTICE, ContextBudget, ContextManager
from tricoder.context.coordinator import MemoryAuditFailure, MemoryCoordinator
from tricoder.context.history import (
    compact_messages,
    compact_session_messages,
    complete_round_tail as _complete_round_tail,
    is_complete_tool_round as _is_complete_tool_round,
)
from tricoder.context.memory import ConversationMemory
from tricoder.context.summarizer import MemorySummarizer, MemorySummaryError
from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import AgentEvent, EventSink, RuntimeFailed
from tricoder.engine.loop import (
    AUDIT_FAILURE_MESSAGE,
    PROVIDER_FAILURE_MESSAGE,
    AgentRunner,
)
from tricoder.engine.provider_request import (
    PLANNING_PROMPT,
    ProviderResponseCollector,
    parse_plan,
    response_events,
)
from tricoder.engine.telemetry import (
    AgentObserver,
    NullObserver,
    ProviderUsageObserver,
    audit_arguments,
    audit_failure_result,
    audit_usage_event,
    elapsed_ms,
    emit,
    log_event,
    notify_provider_usage as _notify_provider_usage,
)
from tricoder.engine.tool_batch import skipped_result
from tricoder.models import (
    MemoryConfig,
    MemoryRefreshResult,
    Message,
    ProviderResponse,
    RunResult,
    SessionContext,
    SessionTurnResult,
    TokenUsage,
    ToolAction,
    ToolDefinition,
    ToolResult,
)
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
from tricoder.providers import ModelProvider
from tricoder.task_cleanup import TaskCleanup, current_cleanup, task_cleanup_scope
from tricoder.task_observation import task_observation_scope
from tricoder.tools import ToolRegistry


# 兼容旧内部异常名称；实现和判断均使用同一个异常类型对象。
_MemoryAuditFailure = MemoryAuditFailure


class CodingAgent:
    """稳定公开入口；单任务阶段编排由 :class:`AgentRunner` 完成。"""

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
        memory_config: MemoryConfig | None = None,
        memory_summarizer: object | None = None,
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
        self._cleanup_owner = TaskCleanup()
        self.max_rounds = max_rounds
        self.max_context_chars = max_context_chars
        self.audit = audit
        self.observer = observer or NullObserver()
        self.tool_protocol = tool_protocol
        self.plan_enabled = plan_enabled
        self.memory_config = memory_config or MemoryConfig()
        self.memory_summarizer = memory_summarizer
        if self.memory_config.compaction == "structured" and self.memory_summarizer is None:
            self.memory_summarizer = MemorySummarizer(provider, self.memory_config)
        self._protocol: ActionProtocol = _PROTOCOLS[tool_protocol]
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
        """一次性运行接口，保持既有返回类型。"""

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

    def refresh_review_memory(
        self,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
    ) -> MemoryRefreshResult:
        """同步刷新待保存候选，不进入业务任务或工具循环。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.refresh_review_memory_async(
                    context,
                    cancellation=cancellation,
                )
            )
        raise RuntimeError("同步记忆刷新入口不能在已运行的事件循环中调用")

    async def refresh_review_memory_async(
        self,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
    ) -> MemoryRefreshResult:
        """显式重试候选生成；全部批次成功后才返回可提交快照。"""

        token = cancellation or CancellationToken()
        result = await self._build_review_memory_candidate(context, token)
        candidate = result.context.review_memory_candidate
        if not isinstance(candidate, ConversationMemory):
            raise MemorySummaryError("会话记忆候选未生成", code="commit")
        if not self._log(
            {
                "status": "memory_review_refreshed",
                "revision": candidate.revision,
                "covered_through": candidate.covered_through,
                "memory_calls": result.memory_calls,
            }
        ):
            raise MemorySummaryError("会话记忆刷新审计失败", code="audit")
        return result

    async def _build_review_memory_candidate(
        self,
        context: SessionContext,
        cancellation: CancellationToken,
    ) -> MemoryRefreshResult:
        """兼容入口；候选构造与正常收尾共用同一个协调器。"""

        return await self._memory_coordinator().build_review_candidate(
            context,
            cancellation,
        )

    async def run_with_context_async(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> SessionTurnResult:
        """在不可变 Context 上异步执行任务，并发布类型化事件。"""

        if self._cleanup_owner.blocks(current_cleanup()) or getattr(
            self.tools,
            "has_pending_cleanup",
            False,
        ):
            result = RunResult(
                False,
                "旧任务资源清理尚未确认，禁止复用执行资源",
                0,
                cleanup_failed=True,
            )
            emit(event_sink, RuntimeFailed("runtime", result.summary))
            return SessionTurnResult(result, context)
        call_cancellation = (
            cancellation.create_child()
            if cancellation is not None
            else CancellationToken()
        )
        with task_cleanup_scope(
            self._retain_task_cleanup,
            worker_owner=self._cleanup_owner,
        ), task_observation_scope():
            try:
                return await self._run_with_context_owned(
                    task,
                    context,
                    cancellation=call_cancellation,
                    event_sink=event_sink,
                )
            except asyncio.CancelledError:
                call_cancellation.cancel()
                raise

    def _retain_task_cleanup(self, scope: TaskCleanup) -> None:
        retain = getattr(self.tools, "retain_cleanup", None)
        if callable(retain):
            retain(scope)
        else:
            scope.handoff_to(self._cleanup_owner)

    async def _run_with_context_owned(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ) -> SessionTurnResult:
        """兼容的内部注入点；实际主循环只做阶段编排。"""

        token = cancellation or CancellationToken()
        return await self._runner().run(
            task,
            context,
            cancellation=token,
            event_sink=event_sink,
        )

    def _runner(self) -> AgentRunner:
        """按当前公开可注入属性创建单任务 Runner。"""

        return AgentRunner(
            provider=self.provider,
            tools=self.tools,
            protocol=self._protocol,
            tool_protocol=self.tool_protocol,
            context_manager=self.context_manager,
            memory_config=self.memory_config,
            memory_summarizer=self.memory_summarizer,
            max_rounds=self.max_rounds,
            plan_enabled=self.plan_enabled,
            audit=self.audit,
            observer=self.observer,
        )

    def _memory_coordinator(self) -> MemoryCoordinator:
        return MemoryCoordinator(
            self.context_manager,
            self.memory_config,
            self.memory_summarizer,
            self.observer.on_error,
            self._log,
            audit_failure_message=AUDIT_FAILURE_MESSAGE,
        )

    async def _request_provider_async(
        self,
        messages: list[Message],
        tools: tuple[ToolDefinition, ...] | list[ToolDefinition],
        cancellation: CancellationToken,
        event_sink: EventSink | None,
    ) -> ProviderResponse:
        """保留旧直接调用入口；Runner 内部应在真实组件位置注入。"""

        return await ProviderResponseCollector(self.provider).request(
            messages,
            tools,
            cancellation,
            event_sink,
        )

    @staticmethod
    def _response_events(response: ProviderResponse) -> tuple[AgentEvent, ...]:
        return response_events(response)

    @staticmethod
    def _emit(event_sink: EventSink | None, event: AgentEvent) -> None:
        emit(event_sink, event)

    @staticmethod
    def _parse_plan(raw: str) -> str | None:
        return parse_plan(raw)

    _skipped_result = staticmethod(skipped_result)

    def _log(self, event: dict[str, Any]) -> bool:
        return log_event(
            self.audit,
            self.observer,
            event,
            failure_message=AUDIT_FAILURE_MESSAGE,
        )

    def _audit_usage(self, round_number: int, usage: TokenUsage) -> bool:
        return self._log(audit_usage_event(round_number, usage))

    @staticmethod
    def _audit_failure_result(
        round_number: int,
        tool_calls: int,
        modified_files: list[str],
        verification: str,
    ) -> RunResult:
        return audit_failure_result(
            AUDIT_FAILURE_MESSAGE,
            round_number,
            tool_calls,
            modified_files,
            verification,
        )

    def _audit_arguments(
        self,
        action: ToolAction,
        result: ToolResult,
    ) -> dict[str, Any]:
        return audit_arguments(self.tools, action, result)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return elapsed_ms(started)
