"""交互会话的运行时装配、原子切换与安全摘要持久化。"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Callable, Concatenate, Mapping, ParamSpec, Protocol, TypeVar

from tricoder.agent import AgentObserver, CodingAgent
from tricoder.execution_state import EffectState, ExecutionState, FileEffects
from tricoder.audit import AuditLogger
from tricoder.changes import (
    ChangeJournal,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
    render_change_set_diff,
)
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.task_cleanup import TaskCleanup, cleanup_scope, current_cleanup
from tricoder.core.events import EventSink
from tricoder.context.spill import SpillError, ToolResultSpillStore
from tricoder.config import AppConfig, ConfigError, load_config, preview_provider_models
from tricoder.models import (
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionMemory,
    SessionRecord,
)
from tricoder.mcp.security import MCP_START_APPROVAL_ACTION
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy
from tricoder.providers import ModelProvider, create_provider
from tricoder.sessions import (
    SessionError,
    SessionStore,
    safe_requirement_summary,
    validate_session_name,
)
from tricoder.tools import ToolContext, ToolRegistry, UndoConflictError
from tricoder.verification import VerificationScope
from tricoder.task_observation import current_task_observation, task_observation_scope


class SessionRuntimeError(RuntimeError):
    """会话装配、切换或内存持久化无法安全完成。"""


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _idle_runtime_change(
    method: Callable[Concatenate["SessionRuntime", _P], _R],
) -> Callable[Concatenate["SessionRuntime", _P], _R]:
    """用任务锁串行化运行时状态变更，消除“先检查、后修改”的竞态。"""

    @wraps(method)
    def guarded(self: "SessionRuntime", *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if not self._task_lock.acquire(blocking=False):
            raise SessionRuntimeError("Agent 任务运行中，禁止切换或修改配置")
        try:
            if self._task_active:
                raise SessionRuntimeError("Agent 任务运行中，禁止切换或修改配置")
            return method(self, *args, **kwargs)
        finally:
            self._task_lock.release()

    return guarded


# fullaccess 级别下仍要求人工审批的“明确危险”工具。
# 当前工具集无删除/重命名能力；未来新增 delete_file、rename_file 等
# 破坏性工具时应加入此集合，fullaccess 下它们仍需审批。
_DANGEROUS_TOOLS: frozenset[str] = frozenset(
    {"dangerous_extension_tool", MCP_START_APPROVAL_ACTION}
)


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """保留启动参数，确保切换和模型变更沿用同一安全边界。"""

    environ: Mapping[str, str] | None = None
    env_file: Path | None = None
    audit_dir: Path | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    max_rounds: int | None = None
    max_context_chars: int | None = None
    timeout: float | None = None
    read_only: bool = False
    plan_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """当前进程内一个会话的独立运行快照。"""

    record: SessionRecord
    memory: SessionMemory
    context: SessionContext
    config: AppConfig
    agent: "ContextAgent"
    tools: ToolRegistry | None = None
    journal: ChangeJournal = field(default_factory=ChangeJournal)
    audit: AuditLogger | None = None


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """供交互层显示的会话与持久化状态，不包含敏感内容。"""

    record: SessionRecord
    unsaved_memory: bool
    warning: str = ""


class ContextAgent(Protocol):
    """运行时只依赖可复用上下文的 Agent 接口，方便注入假实现。"""

    def run_with_context(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ):  # type: ignore[no-untyped-def]
        """运行任务并返回结果与更新后的上下文。"""

    async def run_with_context_async(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: EventSink | None = None,
    ):  # type: ignore[no-untyped-def]
        """在调用方事件循环内运行任务。"""


ActiveSessionFactory = Callable[[SessionRecord, SessionMemory, RuntimeOptions], ActiveSession]
ProviderFactory = Callable[[ProviderConfig, float], ModelProvider]
ModelPreviewResolver = Callable[..., Mapping[str, str]]


def _normalized_verification(value: object) -> str:
    """将可能来自 Agent 的自由文本压缩为可持久化的固定状态。"""
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    if normalized in {"passed", "pass", "通过", "成功"}:
        return "passed"
    if normalized in {"failed", "fail", "失败", "未通过"}:
        return "failed"
    if normalized in {"not-run", "not run", "未运行"}:
        return "not-run"
    if normalized in {"pending", "待验证"}:
        return "待验证"
    return "unknown"


def _persisted_run_summary(result: RunResult, verification: str) -> str:
    """根据受控结构字段生成摘要，绝不拼接任务或模型返回原文。"""
    outcome = "succeeded" if result.ok else "failed"
    return f"run: {outcome}; modified_files={len(result.modified_files)}; verification={verification}"


class SessionRuntime:
    """只在候选配置、策略、工具和 Agent 都成功后替换当前会话。"""

    def __init__(
        self,
        store: SessionStore,
        workspace: Path,
        *,
        options: RuntimeOptions | None = None,
        active_session_factory: ActiveSessionFactory | None = None,
        config_loader: Callable[..., AppConfig] = load_config,
        model_preview_resolver: ModelPreviewResolver = preview_provider_models,
        provider_factory: ProviderFactory = create_provider,
        workspace_policy_factory: Callable[[Path], WorkspacePolicy] = WorkspacePolicy,
        command_policy_factory: Callable[[Path], CommandPolicy] = CommandPolicy,
        tool_registry_factory: Callable[[ToolContext], ToolRegistry] = ToolRegistry,
        agent_factory: Callable[..., ContextAgent] = CodingAgent,
        audit_factory: Callable[[Path], AuditLogger] = AuditLogger,
        approver: Callable[[str, str], bool] | None = None,
        observer: AgentObserver | None = None,
        mcp_manager_factory: Callable[..., object] | None = None,
    ) -> None:
        self.store = store
        self.options = options or RuntimeOptions()
        self._active_session_factory = active_session_factory
        self._config_loader = config_loader
        self._model_preview_resolver = model_preview_resolver
        self._provider_factory = provider_factory
        self._workspace_policy_factory = workspace_policy_factory
        self._command_policy_factory = command_policy_factory
        self._tool_registry_factory = tool_registry_factory
        self._agent_factory = agent_factory
        self._audit_factory = audit_factory
        self._approver = approver or (lambda _action, _detail: False)
        self._observer = observer
        self._mcp_manager_factory = mcp_manager_factory
        self._unsaved_memory = False
        self._warning = ""
        self._memory_dirty = False
        self._session_cache: dict[str, ActiveSession] = {}
        # 每个会话首次在当前进程装配时清理上次进程遗留的临时正文；
        # 同进程内重建模型则保留仍可能被消息引用的结果。
        self._prepared_spill_sessions: set[str] = set()
        # 并发控制：同一时刻只允许一个 Agent 任务，运行期间禁止切换会话/模型/权限。
        self._task_lock = threading.Lock()
        self._task_state_lock = threading.Lock()
        self._task_active = False
        self._task_accepts_cancellation = False
        self._task_session_id: str | None = None
        self._task_permission: str | None = None
        self._task_cancellation: CancellationToken | None = None
        self._pending_cleanup: list[TaskCleanup] = []
        self._shutdown_requested = False

        resolved_workspace = Path(workspace).resolve()
        try:
            self.store.initialize(resolved_workspace)
            record = self.store.latest_for_workspace(resolved_workspace)
            if record is None:
                self.current = self._activate_new_session("default", resolved_workspace)
            else:
                memory = self.store.load_memory(record.id)
                if self._has_startup_override():
                    # 显式启动选项必须先经完整候选构建验证，避免旧会话静默覆盖用户选择。
                    provider = self.options.provider or record.provider
                    model = self.options.model
                    config = self._load_config(record.workspace, provider, model)
                    candidate_record = replace(
                        record,
                        provider=config.provider.name,
                        model=config.provider.model,
                    )
                    candidate = self._build_active(candidate_record, memory, config=config)
                    record = self.store.update_configuration(
                        record.id,
                        config.provider.name,
                        config.provider.model,
                    )
                    self.current = replace(candidate, record=record)
                else:
                    self.current = self._build_active(record, memory)
            self._persisted_memory = self.current.memory
            self._cache_current()
        except (SessionError, ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("无法安全初始化会话运行时") from exc

    @_idle_runtime_change
    def create(self, name: str) -> ActiveSession:
        """以当前工作区和模型创建独立会话；构建成功后才切换。"""
        current = self.current
        try:
            candidate = self._prepare_new_active(
                name,
                current.record.workspace,
                current.record.provider,
                current.record.model,
            )
        except (SessionError, ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("无法创建会话") from exc
        if self._memory_dirty and not self._persist_current():
            raise SessionRuntimeError("当前会话记忆未持久化，已取消创建")
        self._cache_current()
        try:
            record = self.store.insert_prepared(candidate.record)
        except (SessionError, ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("无法创建会话") from exc
        self.current = self._invalidate_verification(self.current)
        self._cache_current()
        self.current = replace(candidate, record=record)
        self._persisted_memory = self.current.memory
        self._memory_dirty = False
        self._clear_unsaved_warning()
        self._cache_current()
        return self.current

    @_idle_runtime_change
    def switch(
        self,
        session_id: str,
        *,
        confirm: Callable[[Path], bool],
    ) -> ActiveSession:
        """确认跨工作区后先保存、完整构建候选，再原子替换当前会话。"""
        original = self.current
        try:
            record = self.store.get(session_id)
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("无法读取目标会话") from exc
        if record.id == original.record.id:
            return original
        if record.workspace != original.record.workspace and not confirm(record.workspace):
            return original
        if self._memory_dirty and not self._persist_current():
            raise SessionRuntimeError("当前会话记忆未持久化，已取消切换")
        self._cache_current()
        candidate = self._session_cache.get(record.id)
        if candidate is None:
            try:
                memory = self.store.load_memory(session_id)
                candidate = self._build_active(record, memory)
            except (SessionError, ConfigError, OSError, ValueError) as exc:
                raise SessionRuntimeError("目标会话构建失败，当前会话未改变") from exc
            self._session_cache[record.id] = candidate
        self.current = self._invalidate_verification(original)
        self._cache_current()
        candidate = self._invalidate_verification(candidate)
        self.current = candidate
        self._cache_current()
        self._persisted_memory = candidate.memory
        self._memory_dirty = False
        self._clear_unsaved_warning()
        return candidate

    @_idle_runtime_change
    def rename_current(self, name: str) -> SessionRecord:
        """先持久化重命名，成功后才替换内存中的会话元数据。"""
        try:
            record = self.store.rename(self.current.record.id, name)
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("会话重命名失败") from exc
        self.current = replace(self.current, record=record)
        self._cache_current()
        return record

    @_idle_runtime_change
    def clear_current(self, *, confirmed: bool = False) -> None:
        """清除消息和摘要，但保留文件路径与验证状态等结构化元数据。"""
        original = self.current
        if (original.memory.unknown_effects or original.context.unknown_effects) and confirmed is not True:
            raise SessionRuntimeError("文件影响未确认；请检查实际文件后明确确认 /clear；清记录不会恢复文件")
        original = self._invalidate_verification(original)
        memory = SessionMemory(
            modified_files=original.memory.modified_files,
            verification=("待验证" if original.memory.unknown_effects else original.memory.verification),
            permission_level=original.memory.permission_level,
        )
        self.current = replace(
            original,
            memory=memory,
            context=SessionContext(
                modified_files=memory.modified_files,
                verification=memory.verification,
            ),
        )
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        persistence_error: SessionError | OSError | None = None
        try:
            if not self._persist_current():
                raise OSError("会话记忆清除未持久化")
        except (SessionError, OSError) as exc:
            # 即使 SQLite 暂时不可写，也继续销毁用户明确要求清除的临时正文。
            # 内存中的清空状态与 dirty 标记会保留，供 retry_persist 后续重试。
            persistence_error = exc
        spill_store = (
            original.tools.context.spill_store
            if original.tools is not None
            else None
        )
        cleanup_error: SpillError | None = None
        if spill_store is not None:
            try:
                spill_store.cleanup()
            except SpillError as exc:
                cleanup_error = exc
        if persistence_error is not None and cleanup_error is not None:
            raise SessionRuntimeError(
                "会话记忆清除未持久化，且大型工具结果清理失败"
            ) from persistence_error
        if persistence_error is not None:
            raise SessionRuntimeError("会话记忆清除未持久化") from persistence_error
        if cleanup_error is not None:
            raise SessionRuntimeError("会话已清空，但大型工具结果清理失败") from cleanup_error

    @_idle_runtime_change
    def change_model(self, provider: str) -> ActiveSession:
        """先验证新配置并构建完整候选，最后才写入会话模型并替换当前值。"""
        original = self.current
        try:
            config = self._load_config(original.record.workspace, provider, self.options.model)
            candidate_record = replace(
                original.record,
                provider=config.provider.name,
                model=config.provider.model,
            )
            rebuilt = self._build_active(
                candidate_record,
                original.memory,
                config=config,
                journal=original.journal,
            )
            candidate = replace(
                rebuilt,
                memory=original.memory,
                context=original.context,
                journal=original.journal,
            )
            persisted = self.store.update_configuration(
                original.record.id,
                config.provider.name,
                config.provider.model,
            )
        except (SessionError, ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("模型切换失败，当前会话未改变") from exc
        self.current = replace(candidate, record=persisted)
        self._cache_current()
        return self.current

    def run_task(self, task: str) -> RunResult:
        """运行后仅提炼安全摘要和结构化元数据，绝不持久化原始消息。

        同一时刻只允许一个 Agent 任务：运行期间拒绝第二个任务，审批使用任务
        启动时的权限快照；任务完成后只允许更新启动该任务的会话。
        """
        if not self._task_lock.acquire(blocking=False):
            raise SessionRuntimeError("已有 Agent 任务正在运行")
        if self._pending_cleanup:
            self._task_lock.release()
            raise SessionRuntimeError("旧任务资源清理尚未确认，禁止复用执行资源")
        if self._task_active:
            self._task_lock.release()
            raise SessionRuntimeError("已有 Agent 任务正在运行")
        cleanup = TaskCleanup()
        try:
            # 令牌与活动标志作为一个快照发布，避免取消线程观察到
            # ``active=True`` 但令牌仍为空的短暂窗口。
            with self._task_state_lock:
                if self._shutdown_requested:
                    raise SessionRuntimeError("Runtime 正在退出，禁止启动新任务")
                self._task_cancellation = CancellationToken()
                self._task_active = True
                self._task_accepts_cancellation = True
            self._task_session_id = self.current.record.id
            self._task_permission = self.current.memory.permission_level
            try:
                with cleanup_scope(cleanup), task_observation_scope():
                    return self._run_task_locked(task)
            finally:
                if self.current.record.id != self._task_session_id:
                    raise SessionRuntimeError(
                        "任务运行期间会话被切换，拒绝更新该会话"
                    )
        finally:
            if cleanup.has_pending:
                self._pending_cleanup.append(cleanup)
            self._finish_task_ownership()

    def _finish_task_ownership(self) -> None:
        """任务所有者消费退出请求；登记、退出快照与释放锁之间不能漏掉交接。"""
        with self._task_state_lock:
            shutdown = self._shutdown_requested
            if not shutdown:
                # 原子发布 idle 并释放互斥锁；之后到达的退出请求必走 idle 清理。
                self._release_task_ownership_locked()
        if shutdown:
            try:
                # 持有任务互斥锁防止资源复用，但绝不持状态/UI 锁跨清理等待。
                self._retry_pending_cleanup_locked()
            finally:
                with self._task_state_lock:
                    self._release_task_ownership_locked()

    def _release_task_ownership_locked(self) -> None:
        self._task_active = False
        self._task_accepts_cancellation = False
        self._task_cancellation = None
        self._task_session_id = None
        self._task_permission = None
        self._task_lock.release()

    def request_shutdown(self) -> bool:
        """一次性记住退出请求，返回由活动任务负责后续清理的原子快照。"""
        with self._task_state_lock:
            self._shutdown_requested = True
            return self._task_active

    def cancel_current(self) -> bool:
        """无须获取任务锁即可线程安全地请求取消当前任务。"""

        with self._task_state_lock:
            token = self._task_cancellation
            if not self._task_active or not self._task_accepts_cancellation or token is None:
                return False
            return token.cancel()

    def _commit_task_outcome(self, result: RunResult) -> RunResult:
        """封存后、持久化前关闭取消接收；已接受的取消必须进入提交结果。"""
        with self._task_state_lock:
            cancelled = bool(self._task_cancellation is not None and self._task_cancellation.is_cancelled)
            self._task_accepts_cancellation = False
        # 状态锁只保护取消/提交决议，不跨文件扫描、账本回调或持久化 I/O。
        if not cancelled:
            return result
        context = self.current.context
        if self.current.tools is not None:
            self.current.tools.context.verification_scope.revoke()
        if context.verification_required or context.verification_evidence is not None or context.verification_failure is not None:
            context = replace(context, verification_evidence=None, verification_required=True,
                              verification="失败" if context.verification_failure is not None else "待验证")
        result = replace(result, ok=False, verification=context.verification,
                         summary="任务已取消" if result.ok else result.summary)
        verification = _normalized_verification(result.verification)
        summary = _persisted_run_summary(result, verification)
        memory = replace(self.current.memory, summary=summary, last_task_summary=summary,
                         verification=verification)
        self.current = replace(self.current, context=context, memory=memory)
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        return result

    def current_task_cancellation(self) -> CancellationToken | None:
        """只读发布当前任务令牌；调用方等待 UI 时不得继续持有状态锁。"""
        with self._task_state_lock:
            return self._task_cancellation if self._task_active else None

    def cleanup_pending_resources(self) -> bool:
        """退出时重试 exact 旧资源；任务互斥锁防复用，状态锁不跨清理等待。"""
        if not self._task_lock.acquire(blocking=False):
            return False
        try:
            return self._retry_pending_cleanup_locked()
        finally:
            self._task_lock.release()

    def _retry_pending_cleanup_locked(self) -> bool:
        # 一次退出重试共享新预算；旧 scope 的失败事实与 exact 资源身份仍然保留。
        deadline = time.monotonic() + 5.0
        self._pending_cleanup = [scope for scope in self._pending_cleanup
                                 if not scope.retry(deadline)]
        return not self._pending_cleanup

    def _run_task_locked(self, task: str) -> RunResult:
        original = self.current
        if original.memory.unknown_effects or original.context.unknown_effects:
            return RunResult(
                False, "文件影响未确认；请检查实际文件并通过 /clear 明确确认", 0,
                modified_files=original.context.modified_files, verification="待验证",
                unknown_effects=True,
            )
        original.journal.begin_task(
            tuple(original.context.modified_files),
            original.context.verification,
        )
        observation = current_task_observation()
        if observation is not None:
            observation.seed(original.context, journal_revision=original.journal.active_revision)
        final_capture_active = False
        try:
            if self._mcp_effectively_enabled(original.config):
                if original.tools is None:
                    raise SessionRuntimeError("启用 MCP 的会话缺少工具注册表")
                # 动态工具仅注册到本任务的新容器；ToolContext 仍是当前会话的
                # 同一安全对象，因此账本、spill、审批快照和审计边界不会漂移。
                task_tools = self._tool_registry_factory(original.tools.context)
                task_agent = self._create_agent(
                    original.config,
                    task_tools,
                    original.audit,
                )

                async def operation(_active_tools: ToolRegistry):
                    return await task_agent.run_with_context_async(
                        task,
                        original.context,
                        cancellation=self._task_cancellation,
                        event_sink=self._observer if callable(self._observer) else None,
                    )

                from tricoder.mcp.runtime import run_mcp_task_sync

                scope_kwargs: dict[str, object] = {}
                if self._mcp_manager_factory is not None:
                    scope_kwargs["manager_factory"] = self._mcp_manager_factory
                turn = run_mcp_task_sync(
                    original.config,
                    task_tools,
                    source_env=(
                        self.options.environ
                        if self.options.environ is not None
                        else os.environ
                    ),
                    audit=original.audit,
                    cancellation=self._task_cancellation or CancellationToken(),
                    operation=operation,
                    **scope_kwargs,
                )
            else:
                # MCP 未启用时保留原有 Agent 对象与同步调用路径。
                run_method = original.agent.run_with_context
                parameters = inspect.signature(run_method).parameters
                accepts_keywords = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                kwargs = {}
                if "cancellation" in parameters or accepts_keywords:
                    kwargs["cancellation"] = self._task_cancellation
                if (
                    ("event_sink" in parameters or accepts_keywords)
                    and callable(self._observer)
                ):
                    kwargs["event_sink"] = self._observer
                turn = run_method(task, original.context, **kwargs)
            observed_context = turn.context
            if observation is not None:
                observed_context, _ = observation.reconcile(observed_context)
            current_snapshot = None
            if (original.tools is not None and original.tools.context.verification_scope.owns(
                    observed_context.verification_evidence)):
                try:
                    # 同步任务所有者持有 cleanup scope 到扫描返回；不创建可迟到的后台工作。
                    final_capture_active = True
                    current_snapshot = original.tools.context.verification_scope.capture(
                        original.tools.context.workspace_policy)
                    final_capture_active = False
                except CancellationError:
                    raise
                except Exception:
                    # 无法取得当前快照只撤销通过，不把扫描异常当成新文件版本。
                    current_snapshot = None
                    final_capture_active = False
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, CancellationError)):
                if self._task_cancellation is not None:
                    self._task_cancellation.cancel()
            interrupted_context = original.context
            observation = current_task_observation()
            if observation is not None:
                interrupted_context, _ = observation.reconcile(interrupted_context)
            if original.tools is not None and original.tools.context.verification_scope.unknown_effects:
                interrupted_context = replace(interrupted_context, unknown_effects=True, verification="待验证",
                                              verification_evidence=None, verification_required=True)
            reconciled = self._reconcile_effects(
                original.journal, interrupted_context, force=True,
                consumed_revision=observation.consumed_revision if observation is not None else None,
            )
            if (final_capture_active
                    or (self._task_cancellation is not None and self._task_cancellation.is_cancelled)
                    or (current_cleanup() is not None and current_cleanup().failed)):
                # 事实可恢复，但取消/清理失败不能恢复可用的通过能力；首异常不变。
                if original.tools is not None:
                    original.tools.context.verification_scope.revoke()
                reconciled = replace(reconciled, verification_evidence=None, verification_required=True,
                                     verification="失败" if reconciled.verification_failure is not None else "待验证")
            memory = replace(
                original.memory, modified_files=reconciled.modified_files,
                verification=reconciled.verification, unknown_effects=reconciled.unknown_effects,
            )
            self.current = replace(original, context=reconciled, memory=memory)
            self._cache_current()
            self._memory_dirty = memory != self._persisted_memory
            try:
                original.journal.seal_task(
                    reconciled.modified_files,
                    reconciled.verification,
                )
            except BaseException:
                # 已有主异常：补偿封存即使被中断也不能替换根因；下方裸 raise 保留首异常。
                pass
            try:
                self._persist_current()
            except BaseException:
                # 仅抑制补偿持久化的次级异常，保留 dirty 状态和原取消/工具异常。
                pass
            raise
        observation = current_task_observation()
        consumed_revision = observation.consumed_revision if observation is not None else None
        reconciled = self._reconcile_effects(
            original.journal, observed_context, force=turn.file_effects_observed is not True,
            consumed_revision=consumed_revision,
        )
        if observation is not None and observation.unknown_effects:
            reconciled = replace(reconciled, unknown_effects=True, verification="待验证",
                                 verification_evidence=None, verification_required=True)
        if turn.result.cleanup_failed or (current_cleanup() is not None and current_cleanup().failed):
            if original.tools is not None:
                original.tools.context.verification_scope.revoke()
            if reconciled.verification_evidence is not None or reconciled.verification_required:
                reconciled = replace(reconciled, verification="待验证", verification_evidence=None,
                                     verification_required=True)
        required = (reconciled.verification_required or reconciled.verification_failure is not None
                    or reconciled.verification_evidence is not None)
        evidence = reconciled.verification_evidence
        trusted_pass = (reconciled.verification_failure is None
                        and reconciled.verification in {"通过", "passed"}
                        and original.tools is not None
                        and original.tools.context.verification_scope.owns(evidence)
                        and evidence.is_valid_for(current_snapshot))
        if required and not trusted_pass:
            reconciled = replace(reconciled, verification_evidence=None, verification_required=True,
                                 verification="失败" if reconciled.verification_failure is not None else "待验证")
        cancelled = bool(self._task_cancellation is not None and self._task_cancellation.is_cancelled)
        if cancelled:
            if original.tools is not None:
                original.tools.context.verification_scope.revoke()
            if required:
                reconciled = replace(reconciled, verification_evidence=None,
                                     verification="失败" if reconciled.verification_failure is not None else "待验证")
        result = replace(
            turn.result, modified_files=reconciled.modified_files,
            summary="任务已取消" if cancelled and turn.result.ok else turn.result.summary,
            cleanup_failed=turn.result.cleanup_failed or bool(
                current_cleanup() is not None and current_cleanup().failed),
            verification=reconciled.verification,
            unknown_effects=turn.result.unknown_effects or reconciled.unknown_effects,
            ok=(turn.result.ok and not cancelled and not reconciled.unknown_effects
                and (not required or trusted_pass)
                and not (reconciled.modified_files and reconciled.verification == "待验证")),
        )
        if result.unknown_effects:
            unknown_notice = "文件影响未确认；请检查实际文件并通过 /clear 明确确认"
            # UNKNOWN 是独立的文件状态事实，不能覆盖用户发起取消这一主终态。
            summary = f"任务已取消；{unknown_notice}" if cancelled else unknown_notice
            result = replace(result, summary=summary, ok=False)
        verification = _normalized_verification(result.verification)
        persisted_summary = _persisted_run_summary(result, verification)
        memory = SessionMemory(
            summary=persisted_summary,
            requirements_summary=safe_requirement_summary(task),
            last_task_summary=persisted_summary,
            modified_files=tuple(result.modified_files),
            verification=verification,
            permission_level=original.memory.permission_level,
            unknown_effects=result.unknown_effects,
        )
        if reconciled.unknown_effects != result.unknown_effects:
            reconciled = replace(reconciled, unknown_effects=result.unknown_effects)
        self.current = replace(original, memory=memory, context=reconciled)
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        try:
            original.journal.seal_task(reconciled.modified_files, reconciled.verification)
        except BaseException:
            try:
                self._persist_current()
            except BaseException:
                # 正常封存已有主异常；补偿持久化中断不能覆盖它，正常持久化不在此边界。
                pass
            raise
        result = self._commit_task_outcome(result)
        self._persist_current()
        return result

    @staticmethod
    def _reconcile_effects(
        journal: ChangeJournal, context: SessionContext, *, force: bool = False,
        consumed_revision: int | None = None,
    ) -> SessionContext:
        """只有显式消费证明才能保留 Agent 验证；路径或对象相同不代表已消费。"""
        try:
            effects = (journal.active_effects() if consumed_revision is None
                       else journal.active_effects_since(consumed_revision))
        except Exception:
            effects = FileEffects(EffectState.UNKNOWN)
        if consumed_revision is None and not force and effects.state is EffectState.CONFIRMED:
            effects = FileEffects(EffectState.NONE)
        state = ExecutionState(context.modified_files, context.verification, context.unknown_effects).observe(effects)
        if effects.state is EffectState.NONE:
            return context
        return replace(context, modified_files=state.modified_files,
                       verification=state.verification, unknown_effects=state.unknown_effects,
                       verification_evidence=None,
                       verification_failure=(None if effects.state is EffectState.CONFIRMED
                                             else context.verification_failure),
                       verification_required=True)

    @staticmethod
    def _invalidate_verification(active: ActiveSession, *, force: bool = False) -> ActiveSession:
        """切会话/撤销只撤销本地能力，不把恢复旧内容解释为恢复旧证明。"""
        context = active.context
        required = (force or context.verification_required or bool(context.modified_files)
                    or context.verification_evidence is not None
                    or context.verification_failure is not None
                    or _normalized_verification(context.verification) in {"passed", "failed", "待验证"})
        if active.tools is not None:
            active.tools.context.verification_scope = VerificationScope()
        if not required:
            return active
        return replace(active,
                       context=replace(context, verification="待验证", verification_evidence=None,
                                       verification_failure=None, verification_required=True),
                       memory=replace(active.memory, verification="待验证"))

    @staticmethod
    def _mcp_effectively_enabled(config: AppConfig) -> bool:
        """只有总开关与至少一个 server 同时启用时才创建任务作用域。"""

        return (
            config.extensions.enabled
            and config.mcp.enabled
            and any(server.enabled for server in config.mcp.servers)
        )

    def diff_latest(self) -> str | None:
        """返回当前 Session 最近一次非空任务的正向差异。"""

        latest = self.current.journal.latest()
        if latest is None:
            return None
        if latest.tainted_paths:
            raise SessionRuntimeError(
                f"无法显示，任务文件状态冲突：{'、'.join(latest.tainted_paths)}"
            )
        return render_change_set_diff(latest)

    @_idle_runtime_change
    def prepare_undo(self) -> UndoPreview:
        """在用户确认前首次全量核验，并只返回反向差异与结构化路径。"""

        change_set, tools = self._undo_inputs()
        paths = tuple(change.path for change in change_set.changes)
        try:
            return tools.preview_undo(change_set)
        except UndoConflictError as exc:
            self._audit_undo(
                status="conflicted",
                paths=paths,
                conflicts=exc.conflicts,
                compensation_status="not-required",
            )
            raise SessionRuntimeError(
                f"无法撤销，文件状态冲突：{'、'.join(exc.conflicts)}"
            ) from None
        except (PolicyError, OSError, UnicodeError, ValueError) as exc:
            self._audit_undo(
                status="failed",
                paths=paths,
                compensation_status="not-required",
            )
            raise SessionRuntimeError("无法安全预览最近任务的撤销") from exc

    @_idle_runtime_change
    def undo_latest(self) -> UndoExecution:
        """执行第二次全量核验；仅在文件全部恢复后提交会话状态。"""

        change_set, tools = self._undo_inputs()
        paths = tuple(sorted(change.path for change in change_set.changes))
        try:
            execution = tools.undo_change_set(change_set)
        except (PolicyError, OSError, UnicodeError, ValueError, TypeError):
            self._audit_undo(
                status="failed",
                paths=paths,
                compensation_status="not-required",
            )
            raise SessionRuntimeError("无法安全执行最近任务的撤销") from None
        if not execution.ok:
            self._audit_undo(
                status="conflicted" if execution.conflicts else "failed",
                paths=execution.paths,
                conflicts=execution.conflicts,
                compensation_failed=execution.compensation_failed,
                compensation_status=(
                    "failed"
                    if execution.compensation_failed
                    else "not-required"
                    if execution.conflicts
                    else "succeeded"
                ),
            )
            return execution

        self.current.journal.clear_latest()
        self.current = self._invalidate_verification(self.current, force=True)
        memory = replace(
            self.current.memory,
            modified_files=change_set.before_modified_files,
            verification="待验证",
        )
        context = replace(
            self.current.context,
            modified_files=change_set.before_modified_files,
            verification="待验证",
        )
        self.current = replace(self.current, memory=memory, context=context)
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        self._audit_undo(
            status="succeeded",
            paths=execution.paths,
            compensation_status="not-required",
        )
        self._persist_current()
        return execution

    def _audit_undo(
        self,
        *,
        status: str,
        paths: tuple[str, ...],
        compensation_status: str,
        conflicts: tuple[str, ...] = (),
        compensation_failed: tuple[str, ...] = (),
    ) -> None:
        """只以结构化规范路径记录撤销结果，不写入源码或异常文本。"""

        if self.current.audit is None:
            return
        event: dict[str, object] = {
            "event": "undo",
            "status": status,
            "paths": paths,
            "file_count": len(paths),
            "conflict_count": len(conflicts),
            "compensation_status": compensation_status,
        }
        if conflicts:
            event["conflicts"] = conflicts
        if compensation_failed:
            event["compensation_failed"] = compensation_failed
        self.current.audit.log(event)

    def _undo_inputs(self) -> tuple[TaskChangeSet, ToolRegistry]:
        """在任何预览、审批或写入前统一拒绝不可撤销状态。"""

        if self.current.config.read_only:
            raise SessionRuntimeError("只读模式禁止撤销")
        if self.current.memory.unknown_effects or self.current.context.unknown_effects:
            raise SessionRuntimeError("文件影响未确认，拒绝撤销；请检查实际文件并通过 /clear 明确确认")
        change_set = self.current.journal.latest()
        if change_set is None:
            raise SessionRuntimeError("当前 Session 没有可撤销的最近任务")
        if self.current.tools is None:
            raise SessionRuntimeError("当前 Session 未装配可撤销工具")
        return change_set, self.current.tools

    @_idle_runtime_change
    def persist_current(self) -> bool:
        """尝试保存安全摘要；失败时保留内存状态并暴露未保存警告。"""
        return self._persist_current()

    def _persist_current(self) -> bool:
        """调用方持有任务锁时执行实际持久化。"""
        if not self._memory_dirty:
            return True
        try:
            self.store.save_memory(self.current.record.id, self.current.memory)
        except (SessionError, OSError):
            self._mark_unsaved()
            return False
        self._persisted_memory = self.current.memory
        self._memory_dirty = False
        self._clear_unsaved_warning()
        return True

    @_idle_runtime_change
    def retry_persist(self) -> bool:
        """供退出流程再尝试一次保存，失败由调用方返回非零退出码。"""
        return self._persist_current()

    @property
    def permission_level(self) -> str:
        """当前会话的权限级别：strict（默认）、relaxed 或 fullaccess。"""
        return self.current.memory.permission_level

    @_idle_runtime_change
    def set_permission(self, level: str | None) -> str:
        """查看或切换当前会话的权限级别并持久化。

        relaxed 只自动放行 git 只读命令；fullaccess 放行全部非危险工具
        （明确非沙盒）。持久化失败时保留内存状态并抛出异常，绝不谎报成功。
        """
        if level is None:
            return self.permission_level
        normalized = level.strip().lower()
        if normalized not in {"strict", "relaxed", "fullaccess"}:
            raise SessionRuntimeError("permission 只能是 strict、relaxed 或 fullaccess")
        current = self.current
        original_dirty = self._memory_dirty
        original_unsaved = self._unsaved_memory
        original_warning = self._warning
        memory = replace(current.memory, permission_level=normalized)
        self.current = replace(current, memory=memory)
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        if not self._persist_current():
            # 权限是安全状态：数据库未提交时，内存也必须恢复旧值。
            self.current = current
            self._cache_current()
            self._memory_dirty = original_dirty
            self._unsaved_memory = original_unsaved
            self._warning = original_warning
            raise SessionRuntimeError("权限持久化失败，已恢复原权限")
        return normalized

    def _effective_approver(self, action: str, detail: str) -> bool:
        """按任务启动时快照的权限级别决定审批策略。

        - strict：全部交回人工审批；
        - relaxed：不自动放行任何命令（git 只读命令由工具层 auto_approve 处理）；
        - fullaccess：放行全部非危险工具（明确非沙盒；命令仍受 CommandPolicy
          白名单、read_only/敏感路径等硬边界约束）。
        审批使用任务启动时的权限快照，不在执行中读取另一个会话的 current。
        """
        permission = self._task_permission or self.current.memory.permission_level
        if (
            permission == "fullaccess"
            and action not in _DANGEROUS_TOOLS
        ):
            return True
        return self._approver(action, detail)

    def _auto_approve_git_command(self, args: list[str]) -> bool:
        """relaxed 仅自动放行策略明确分类为安全的 Git 元数据查询。"""
        if (self._task_permission or self.current.memory.permission_level) != "relaxed":
            return False
        return CommandPolicy.is_relaxed_git_metadata_command(args)

    def status(self) -> RuntimeStatus:
        """返回不含任务原文、工具输出或凭据的状态快照。"""
        return RuntimeStatus(self.current.record, self._unsaved_memory, self._warning)

    def preview_models(self) -> dict[str, str]:
        """只解析模型名供 UI 展示；不读取密钥文件、不校验 Key、也不构建 Provider。"""
        try:
            previews = dict(
                self._model_preview_resolver(
                    self.current.record.workspace,
                    environ=self.options.environ,
                    model=self.options.model,
                )
            )
        except (ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("无法解析模型预览") from exc
        previews[self.current.record.provider] = self.current.record.model
        return previews

    def _activate_new_session(self, name: str, workspace: Path) -> ActiveSession:
        """先构建未持久化候选，成功后才创建 Session 与空记忆记录。"""
        provider = self.options.provider or "openai"
        candidate = self._prepare_new_active(name, workspace, provider, self.options.model)
        self.store.insert_prepared(candidate.record)
        return candidate

    def _prepare_new_active(
        self,
        name: str,
        workspace: Path,
        provider: str,
        model: str | None,
    ) -> ActiveSession:
        """构建不写 SQLite 的临时候选，供创建和首次启动共用。"""
        # 名称必须在任何配置、审计或 Agent 构建前校验，拒绝无效输入的副作用。
        validated_name = validate_session_name(name)
        if self._active_session_factory is None:
            config = self._load_config(workspace, provider, model)
            record = self.store.prepare_record(
                validated_name,
                config.workspace,
                config.provider.name,
                config.provider.model,
            )
            return self._build_active(record, SessionMemory(), config=config)
        record = self.store.prepare_record(
            validated_name,
            workspace,
            provider,
            model or "default",
        )
        return self._build_active(record, SessionMemory())

    def _has_startup_override(self) -> bool:
        """仅在用户显式提供 Provider 或模型时覆盖已恢复的会话配置。"""
        return self.options.provider is not None or self.options.model is not None

    def _build_active(
        self,
        record: SessionRecord,
        memory: SessionMemory,
        *,
        config: AppConfig | None = None,
        journal: ChangeJournal | None = None,
    ) -> ActiveSession:
        """构建完整候选对象，调用方在成功返回前不会修改 ``current``。"""
        # SQLite 只保存展示字符串；加载时没有对应的本地文件版本证明。
        if _normalized_verification(memory.verification) in {"passed", "failed"}:
            memory = replace(memory, verification="待验证")
        if self._active_session_factory is not None:
            candidate = self._active_session_factory(record, memory, self.options)
            active_journal = journal if journal is not None else candidate.journal
            registries: list[ToolRegistry] = []
            for possible in (
                candidate.tools,
                getattr(candidate.agent, "tools", None),
                getattr(candidate.agent, "registry", None),
            ):
                if isinstance(possible, ToolRegistry) and all(
                    possible is not registry for registry in registries
                ):
                    registries.append(possible)
            for registry in registries:
                try:
                    registry.context.change_journal = active_journal
                except (AttributeError, TypeError) as exc:
                    raise SessionRuntimeError("自定义会话工厂无法绑定现有变更账本") from exc
                if registry.context.change_journal is not active_journal:
                    raise SessionRuntimeError("自定义会话工厂无法绑定现有变更账本")
            return replace(
                candidate,
                context=replace(candidate.context, unknown_effects=memory.unknown_effects or candidate.context.unknown_effects),
                tools=candidate.tools or (registries[0] if registries else None),
                journal=active_journal,
            )
        loaded = config or self._load_config(record.workspace, record.provider, record.model)
        workspace_policy = self._workspace_policy_factory(loaded.workspace)
        active_journal = journal or ChangeJournal()
        spill_store = ToolResultSpillStore(
            self.store.database_path.parent / "runtime" / "tool-results",
            record.id,
        )
        if record.id not in self._prepared_spill_sessions:
            spill_store.cleanup()
            self._prepared_spill_sessions.add(record.id)
        tools = self._tool_registry_factory(
            ToolContext(
                workspace_policy=workspace_policy,
                command_policy=self._command_policy_factory(loaded.workspace),
                approver=self._effective_approver,
                auto_approve_git=self._auto_approve_git_command,
                read_only=loaded.read_only,
                timeout=loaded.timeout,
                change_journal=active_journal,
                spill_store=spill_store,
            )
        )
        if loaded.audit_dir is None:
            raise SessionRuntimeError("运行配置缺少审计目录")
        audit = self._audit_factory(loaded.audit_dir / f"session-{record.id}.jsonl")
        audit.prepare()
        agent = self._create_agent(loaded, tools, audit)
        return ActiveSession(
            record,
            memory,
            SessionContext(
                persisted_summary=memory.summary,
                modified_files=memory.modified_files,
                verification=memory.verification,
                unknown_effects=memory.unknown_effects,
            ),
            loaded,
            agent,
            tools,
            active_journal,
            audit,
        )

    def _create_agent(
        self,
        config: AppConfig,
        tools: ToolRegistry,
        audit: AuditLogger | None,
    ) -> ContextAgent:
        """按同一 Provider 配置构造长期或任务级 Agent。"""

        provider = self._provider_factory(config.provider, config.timeout)
        agent_kwargs = {
            "max_rounds": config.max_rounds,
            "max_context_chars": config.max_context_chars,
            "audit": audit,
            "observer": self._observer,
        }
        agent_parameters = inspect.signature(self._agent_factory).parameters
        if "tool_protocol" in agent_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in agent_parameters.values()
        ):
            agent_kwargs["tool_protocol"] = config.tool_protocol
        if "plan_enabled" in agent_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in agent_parameters.values()
        ):
            agent_kwargs["plan_enabled"] = config.plan_enabled
        return self._agent_factory(provider, tools, **agent_kwargs)

    def _load_config(self, workspace: Path, provider: str, model: str | None) -> AppConfig:
        """集中传递启动选项，避免切换时遗漏只读、审计或资源限制。"""
        return self._config_loader(
            provider=provider,
            workspace=workspace,
            environ=self.options.environ,
            env_file=self.options.env_file,
            audit_dir=self.options.audit_dir,
            model=model,
            base_url=self.options.base_url,
            max_rounds=self.options.max_rounds,
            max_context_chars=self.options.max_context_chars,
            timeout=self.options.timeout,
            read_only=self.options.read_only,
            plan_enabled=self.options.plan_enabled,
        )

    def _mark_unsaved(self) -> None:
        self._unsaved_memory = True
        self._warning = "本次记忆未持久化"

    def _clear_unsaved_warning(self) -> None:
        self._unsaved_memory = False
        self._warning = ""

    def _cache_current(self) -> None:
        """按稳定 ID 保存完整进程内快照，切换时不得跨会话复用对象。"""
        self._session_cache[self.current.record.id] = self.current
