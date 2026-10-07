"""交互会话的运行时装配、原子切换与安全摘要持久化实现。"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
import time
import secrets
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Callable, Concatenate, Mapping, ParamSpec, Protocol, TypeVar

from tricoder.agent import AgentObserver, CodingAgent
from tricoder.execution_state import EffectState, FileEffects
from tricoder.audit import AuditLogger
from tricoder.changes import (
    ChangeJournal,
    DirectoryChange,
    FileChange,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
    render_change_set_diff,
)
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.clarification import Clarifier
from tricoder.task_cleanup import TaskCleanup, cleanup_scope, current_cleanup
from tricoder.core.events import EventSink
from tricoder.context.spill import SpillError, ToolResultSpillStore
from tricoder.context.summarizer import (
    MemorySummaryError,
    memory_summary_failure_code,
    memory_summary_failure_label,
)
from tricoder.context.memory import (
    ArchivedMemoryItem,
    ConversationMemory,
    MAX_ARCHIVED_ITEMS,
    MemoryItem,
    MemoryValidationError,
    memory_source_ids,
    memory_to_json,
    source_id_for_sequence,
    validate_candidate,
)
from tricoder.config import AppConfig, ConfigError, load_config, preview_provider_models
from tricoder.models import (
    ProviderConfig,
    MemoryRefreshResult,
    Message,
    RunResult,
    SessionContext,
    SessionMemory,
    SessionRecord,
)
from tricoder.mcp.security import MCP_START_APPROVAL_ACTION
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy
from tricoder.providers import ModelProvider, create_provider
from tricoder.session.lock import SessionLock, SessionLockBusyError
from tricoder.session.store import (
    SessionError,
    SessionStore,
    safe_requirement_summary,
    validate_session_name,
)
from tricoder.tools import ToolContext, ToolRegistry, UndoConflictError
from tricoder.workspace.verification import VerificationScope, stable_snapshots
from tricoder.task_observation import (
    apply_tool_transition,
    current_task_observation,
    task_observation_scope,
)
from tricoder.workspace.lock import (
    WorkspaceIdentityError,
    WorkspaceLock,
    WorkspaceLockBusyError,
    WorkspaceLockError,
    WorkspaceRecoveryRequiredError,
)
from tricoder.workspace.gate import (
    WorkspaceConfirmationRejected,
    WorkspaceConfirmationUnavailable,
    WorkspaceGate,
    WorkspaceGateCancelled,
    WorkspaceGateError,
    WorkspaceGatePreview,
)
from tricoder.workspace.snapshot import (
    SnapshotLimits,
    WorkspaceBaseline,
    WorkspaceScanError,
    capture_workspace_baseline,
    compare_baselines,
    task_changes_match_baselines,
)


class SessionRuntimeError(RuntimeError):
    """会话装配、切换或内存持久化无法安全完成。"""


class SessionInUseError(SessionRuntimeError):
    """可直接展示的固定占用提示，不包含底层路径或异常。"""

    def __init__(self) -> None:
        super().__init__("该会话已被其他终端占用，请先在原终端切换会话或退出")


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
            if self._closed or self._close_requested:
                raise SessionRuntimeError("Runtime 已关闭或正在退出，禁止修改会话")
            if self._task_active:
                raise SessionRuntimeError("Agent 任务运行中，禁止切换或修改配置")
            if (
                self._pending_undo_binding is not None
                and method.__name__ not in {"undo_latest", "cancel_undo"}
            ):
                raise SessionRuntimeError("撤销预览等待确认，禁止修改会话或启动其他操作")
            return method(self, *args, **kwargs)
        finally:
            # 与 close() 原子交接：检查退出标记后到释放任务锁之间不能漏接请求。
            with self._task_state_lock:
                closing = self._close_requested and not self._closed
                if not closing:
                    self._task_lock.release()
            if closing:
                try:
                    self._close_locked()
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
class _WorkspaceUndoBinding:
    """只绑定撤销预览后的完整工作区版本，不保存额外源码副本。"""

    session_id: str
    generation: int
    snapshot_id: str
    baseline: WorkspaceBaseline = field(repr=False)
    change_set: TaskChangeSet = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """供交互层显示的会话与持久化状态，不包含敏感内容。"""

    record: SessionRecord | None
    unsaved_memory: bool
    warning: str = ""
    workspace: Path | None = None
    provider: str = ""
    model: str = ""
    read_only: bool = False
    permission_level: str = "strict"
    verification: str = "未运行"
    context_messages: int = 0
    modified_files: int = 0
    modified_directories: int = 0


@dataclass(frozen=True, slots=True)
class MemorySavePreview:
    """用户确认的确切保存候选；candidate 不进入 repr 或审计。"""

    session_id: str
    generation: int
    revision: int
    persisted_revision: int | None
    next_message_seq: int
    text: str
    original_memory: ConversationMemory = field(repr=False)
    original_review_candidate: ConversationMemory | None = field(repr=False)
    candidate: ConversationMemory = field(repr=False)


@dataclass(frozen=True, slots=True)
class MemoryEditPreview:
    """本地编辑的 revision 绑定预览。"""

    session_id: str
    generation: int
    revision: int
    next_message_seq: int
    text: str
    original_memory: ConversationMemory = field(repr=False)
    original_review_candidate: ConversationMemory | None = field(repr=False)
    targets_review_candidate: bool
    candidate: ConversationMemory = field(repr=False)


@dataclass(frozen=True, slots=True)
class MemoryArchiveDeletePreview:
    """归档逐项删除的确切预览；只删除归档，不触碰同名活跃内容。"""

    session_id: str
    generation: int
    revision: int
    next_message_seq: int
    text: str
    original_memory: ConversationMemory = field(repr=False)
    original_review_candidate: ConversationMemory | None = field(repr=False)
    targets_review_candidate: bool
    archived_item: ArchivedMemoryItem = field(repr=False)
    candidate: ConversationMemory = field(repr=False)


_SENSITIVE_MEMORY_PATTERN = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret|credential)\s*[:=])",
    re.IGNORECASE,
)


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

    def refresh_review_memory(
        self,
        context: SessionContext,
        *,
        cancellation: CancellationToken | None = None,
    ) -> MemoryRefreshResult:
        """仅整理待保存候选，不进入业务工具循环。"""


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
        clarifier: Clarifier | None = None,
        observer: AgentObserver | None = None,
        mcp_manager_factory: Callable[..., object] | None = None,
        initial_session_id: str | None = None,
        workspace_confirmer: Callable[[WorkspaceGatePreview], bool] | None = None,
        workspace_snapshotter: Callable[..., WorkspaceBaseline] = capture_workspace_baseline,
        workspace_snapshot_limits: SnapshotLimits | None = None,
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
        self._clarifier = clarifier
        self._observer = observer
        self._mcp_manager_factory = mcp_manager_factory
        self._workspace_confirmer = workspace_confirmer
        self._workspace_gate = WorkspaceGate(
            snapshotter=workspace_snapshotter,
            limits=workspace_snapshot_limits,
        )
        self._workspace_baselines: dict[str, WorkspaceBaseline] = {}
        self._baseline_confirmation_required: set[str] = set()
        self._workspace_gate_generation = 0
        self._workspace_baseline_unresolved: set[str] = set()
        self._task_sealed_change_set: TaskChangeSet | None = None
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
        self._close_requested = False
        self._closed = False
        self._close_ok = True
        self._session_lock: SessionLock | None = None
        self._workspace_lock: WorkspaceLock | None = None
        self._pending_undo_binding: _WorkspaceUndoBinding | None = None

        resolved_workspace = Path(workspace).resolve()
        self._entry_workspace = resolved_workspace
        self._entry_provider = self.options.provider or "openai"
        self._entry_model = self.options.model
        self._entry_permission = "strict"
        self.current: ActiveSession | None = None
        self._persisted_memory = SessionMemory()
        try:
            self.store.initialize(resolved_workspace)
            if initial_session_id is not None:
                record = self.store.get(initial_session_id)
                self._session_lock = self._acquire_session(record.id)
                # 获取锁前只能用旧记录确定 ID；所有可变元数据在取得所有权后重读。
                record = self.store.get(record.id)
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
                self._baseline_confirmation_required.add(record.id)
        except BaseException as exc:
            if self._session_lock is not None:
                self._session_lock.close()
            if isinstance(exc, (SessionError, ConfigError, OSError, ValueError)):
                raise SessionRuntimeError("无法安全初始化会话运行时") from exc
            raise

    @property
    def has_active_session(self) -> bool:
        """当前 Runtime 是否已经取得一个真实 Session 的所有权。"""

        return self.current is not None

    def _require_active_session(self) -> ActiveSession:
        """返回当前真实 Session；入口状态使用稳定业务错误而非属性异常。"""

        if self.current is None:
            raise SessionRuntimeError("尚未创建或选择会话")
        return self.current

    def _acquire_session(self, session_id: str) -> SessionLock:
        try:
            return SessionLock(self.store.database_path, session_id)
        except SessionLockBusyError as exc:
            raise SessionInUseError() from exc
        except OSError as exc:
            raise SessionRuntimeError("无法安全锁定目标会话，当前会话未改变") from exc

    def _handoff_session(self, candidate: ActiveSession, ownership: SessionLock) -> None:
        """目标装配与旧记忆保存完成后交接；释放前丢弃旧会话缓存。"""
        original = self.current
        previous_lock = self._session_lock
        candidate = self._invalidate_verification(candidate)
        if original is not None:
            self._invalidate_verification(original)
        self.current = candidate
        self._session_lock = ownership
        self._persisted_memory = candidate.memory
        self._memory_dirty = False
        self._clear_unsaved_warning()
        self._cache_current()
        if original is not None:
            self._prepared_spill_sessions.discard(original.record.id)
        if previous_lock is not None:
            previous_lock.close()

    @_idle_runtime_change
    def create(self, name: str) -> ActiveSession:
        """以当前工作区和模型创建独立会话；构建成功后才切换。"""
        current = self.current
        if self._pending_cleanup:
            raise SessionRuntimeError("旧任务资源清理尚未确认，禁止释放当前会话")
        try:
            candidate, ownership = self._prepare_new_active(
                name,
                current.record.workspace if current is not None else self._entry_workspace,
                current.record.provider if current is not None else self._entry_provider,
                current.record.model if current is not None else self._entry_model,
                memory=SessionMemory(
                    permission_level=(
                        current.memory.permission_level
                        if current is not None
                        else self._entry_permission
                    )
                ),
            )
        except (SessionError, ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("无法创建会话") from exc
        try:
            if current is not None and not self._retry_persist_locked():
                raise SessionRuntimeError("当前会话记忆未持久化，已取消创建")
            record = self.store.insert_prepared(candidate.record)
        except BaseException as exc:
            ownership.close()
            self._prepared_spill_sessions.discard(candidate.record.id)
            if isinstance(exc, (SessionError, ConfigError, OSError, ValueError)):
                raise SessionRuntimeError("无法创建会话") from exc
            raise
        self._handoff_session(replace(candidate, record=record), ownership)
        if current is not None:
            self._workspace_baselines.pop(current.record.id, None)
            self._baseline_confirmation_required.discard(current.record.id)
            self._workspace_baseline_unresolved.discard(current.record.id)
        return self._require_active_session()

    @_idle_runtime_change
    def switch(
        self,
        session_id: str,
        *,
        confirm: Callable[[Path], bool],
    ) -> ActiveSession:
        """确认跨工作区后先保存、完整构建候选，再原子替换当前会话。"""
        original = self.current
        if self._pending_cleanup:
            raise SessionRuntimeError("旧任务资源清理尚未确认，禁止释放当前会话")
        try:
            record = self.store.get(session_id)
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("无法读取目标会话") from exc
        if original is not None and record.id == original.record.id:
            return original
        current_workspace = (
            original.record.workspace if original is not None else self._entry_workspace
        )
        if record.workspace != current_workspace and not confirm(record.workspace):
            if original is None:
                raise SessionRuntimeError("已取消切换，当前仍未选择会话")
            return original
        ownership = self._acquire_session(record.id)
        try:
            record = self.store.get(record.id)
            memory = self.store.load_memory(record.id)
            candidate = self._build_active(record, memory)
            if original is not None and not self._retry_persist_locked():
                raise SessionRuntimeError("当前会话记忆未持久化，已取消切换")
        except BaseException as exc:
            ownership.close()
            self._prepared_spill_sessions.discard(record.id)
            if isinstance(exc, (SessionError, ConfigError, OSError, ValueError)):
                raise SessionRuntimeError("目标会话构建失败，当前会话未改变") from exc
            raise
        self._handoff_session(candidate, ownership)
        if original is not None:
            self._workspace_baselines.pop(original.record.id, None)
            self._baseline_confirmation_required.discard(original.record.id)
            self._workspace_baseline_unresolved.discard(original.record.id)
        self._baseline_confirmation_required.add(candidate.record.id)
        return self._require_active_session()

    @_idle_runtime_change
    def rename_current(self, name: str) -> SessionRecord:
        """先持久化重命名，成功后才替换内存中的会话元数据。"""
        current = self._require_active_session()
        try:
            record = self.store.rename(current.record.id, name)
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("会话重命名失败") from exc
        self.current = replace(current, record=record)
        self._cache_current()
        return record

    @_idle_runtime_change
    def clear_current(self, *, confirmed: bool = False) -> None:
        """清除消息和摘要，但保留文件路径与验证状态等结构化元数据。"""
        original = self._require_active_session()
        if (original.memory.unknown_effects or original.context.unknown_effects) and confirmed is not True:
            raise SessionRuntimeError("文件影响未确认；请检查实际文件后明确确认 /clear；清记录不会恢复文件")
        original = self._invalidate_verification(original)
        memory = SessionMemory(
            modified_files=original.memory.modified_files,
            verification=("待验证" if original.memory.unknown_effects else original.memory.verification),
            permission_level=original.memory.permission_level,
        )
        previous_conversation = original.context.conversation_memory
        if not isinstance(previous_conversation, ConversationMemory):
            raise SessionRuntimeError("会话记忆状态无效")
        cleared_conversation = ConversationMemory(
            generation=previous_conversation.generation + 1,
        )
        self.current = replace(
            original,
            memory=memory,
            context=SessionContext(
                modified_files=memory.modified_files,
                modified_directories=original.context.modified_directories,
                verification=memory.verification,
                conversation_memory=cleared_conversation,
                next_message_seq=original.context.next_message_seq,
                persisted_memory_revision=original.context.persisted_memory_revision,
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
        semantic_clear_error: SessionError | OSError | None = None
        try:
            self.store.clear_conversation_memory(original.record.id)
        except (SessionError, OSError) as exc:
            semantic_clear_error = exc
            self.current = replace(
                self.current,
                context=replace(self.current.context, memory_pending_clear=True),
            )
            self._cache_current()
            self._unsaved_memory = True
            self._warning = "会话记忆持久化清除失败；内存已清空，可本地重试"
        else:
            self.current = replace(
                self.current,
                context=replace(
                    self.current.context,
                    persisted_memory_revision=None,
                    memory_pending_clear=False,
                ),
            )
            self._cache_current()
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
        if semantic_clear_error is not None and cleanup_error is not None:
            raise SessionRuntimeError(
                "会话记忆持久化清除失败，且大型工具结果清理失败"
            ) from semantic_clear_error
        if semantic_clear_error is not None:
            raise SessionRuntimeError("会话记忆持久化清除失败；内存已清空，可重试") from semantic_clear_error
        if persistence_error is not None and cleanup_error is not None:
            raise SessionRuntimeError(
                "会话记忆清除未持久化，且大型工具结果清理失败"
            ) from persistence_error
        if persistence_error is not None:
            raise SessionRuntimeError("会话记忆清除未持久化") from persistence_error
        if cleanup_error is not None:
            raise SessionRuntimeError("会话已清空，但大型工具结果清理失败") from cleanup_error

    @_idle_runtime_change
    def change_model(self, provider: str) -> ActiveSession | None:
        """先验证新配置并构建完整候选，最后才写入会话模型并替换当前值。"""
        original = self.current
        if original is None:
            previews = self.preview_models()
            if provider not in previews:
                raise SessionRuntimeError("模型切换失败，入口配置未改变")
            self._entry_provider = provider
            self._entry_model = previews[provider]
            return None
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
        if not isinstance(task, str) or not task.strip():
            raise SessionRuntimeError("任务不能为空")
        if self._pending_undo_binding is not None:
            raise SessionRuntimeError("撤销预览等待确认，请先确认或取消撤销")
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
            try:
                task_workspace = (
                    self.current.record.workspace
                    if self.current is not None
                    else self._entry_workspace
                )
                self._workspace_lock = WorkspaceLock.acquire(task_workspace)
            except WorkspaceLockBusyError as exc:
                raise SessionRuntimeError("工作区正在执行其他任务") from exc
            except WorkspaceRecoveryRequiredError as exc:
                raise SessionRuntimeError(
                    "检测到上次任务未确认清理；请确认没有遗留进程后恢复活动标记"
                ) from exc
            except (WorkspaceIdentityError, WorkspaceLockError, OSError) as exc:
                raise SessionRuntimeError("无法安全取得工作区任务锁") from exc
            self._workspace_gate_generation += 1
            gate_generation = self._workspace_gate_generation
            request_id = secrets.token_hex(16)
            session_before_gate = self.current
            gate_session_id = (
                session_before_gate.record.id
                if session_before_gate is not None
                else f"pending:{request_id}"
            )
            try:
                gate = self._workspace_gate.enter(
                    task_workspace,
                    baseline=(
                        self._workspace_baselines.get(session_before_gate.record.id)
                        if session_before_gate is not None
                        else None
                    ),
                    require_initialization_confirmation=(
                        session_before_gate is not None
                        and session_before_gate.record.id in self._baseline_confirmation_required
                    ),
                    session_id=gate_session_id,
                    request_id=request_id,
                    generation=gate_generation,
                    confirmer=self._workspace_confirmer,
                    cancellation=self._task_cancellation,
                    change_observer=self._observe_workspace_change,
                )
            except WorkspaceScanError as exc:
                if exc.reason == "cancelled":
                    return RunResult(False, "任务已取消", 0)
                raise SessionRuntimeError(
                    f"工作区扫描失败（{exc.reason}），任务未启动"
                ) from exc
            except WorkspaceConfirmationUnavailable as exc:
                raise SessionRuntimeError("当前入口无法确认工作区变化，任务未启动") from exc
            except WorkspaceConfirmationRejected as exc:
                if exc.kind == "initialize":
                    raise SessionRuntimeError("已拒绝初始化恢复会话的工作区基线") from exc
                raise SessionRuntimeError("已拒绝工作区变化，任务未启动") from exc
            except WorkspaceGateCancelled as exc:
                return RunResult(False, "任务已取消", 0)
            except WorkspaceGateError as exc:
                raise SessionRuntimeError(str(exc)) from exc
            active = self._ensure_session_for_task_locked()
            self._workspace_baselines[active.record.id] = gate.baseline
            self._baseline_confirmation_required.discard(active.record.id)
            self._workspace_baseline_unresolved.discard(active.record.id)
            if gate.changed and gate.preview is not None:
                active = self._with_workspace_change_notice(active, gate.preview)
                self.current = active
                self._cache_current()
            try:
                self._workspace_lock.mark_active()
            except (WorkspaceRecoveryRequiredError, WorkspaceIdentityError, WorkspaceLockError) as exc:
                raise SessionRuntimeError("无法安全建立工作区任务活动标记") from exc
            self._task_session_id = active.record.id
            self._task_permission = active.memory.permission_level
            try:
                with self._task_state_lock:
                    cancelled = bool(
                        self._task_cancellation is not None
                        and self._task_cancellation.is_cancelled
                    )
                    shutting_down = self._shutdown_requested
                if cancelled or shutting_down:
                    return RunResult(
                        False,
                        "任务已取消",
                        0,
                        modified_files=active.context.modified_files,
                        modified_directories=active.context.modified_directories,
                        verification=active.context.verification,
                        unknown_effects=active.context.unknown_effects,
                    )
                with cleanup_scope(cleanup), task_observation_scope():
                    self._task_sealed_change_set = None
                    try:
                        result = self._run_task_locked(task)
                    except BaseException:
                        self._finalize_workspace_exception(active)
                        self._clear_workspace_change_notice()
                        raise
                    return self._finalize_workspace_task(active, result)
            finally:
                current = self._require_active_session()
                if current.record.id != self._task_session_id:
                    raise SessionRuntimeError(
                        "任务运行期间会话被切换，拒绝更新该会话"
                    )
        finally:
            if cleanup.has_pending:
                self._pending_cleanup.append(cleanup)
            try:
                self._finish_task_ownership()
            finally:
                self._release_workspace_lock_if_safe()

    def refresh_memory(self) -> MemoryRefreshResult:
        """串行刷新待保存候选；失败或取消时不提交迟到或部分结果。"""

        original = self._require_active_session()
        if not self._task_lock.acquire(blocking=False):
            raise SessionRuntimeError("已有 Agent 任务正在运行")
        if self._pending_cleanup:
            self._task_lock.release()
            raise SessionRuntimeError("旧任务资源清理尚未确认，禁止刷新会话记忆")
        if self._task_active:
            self._task_lock.release()
            raise SessionRuntimeError("已有 Agent 任务正在运行")
        try:
            with self._task_state_lock:
                if self._shutdown_requested:
                    raise SessionRuntimeError("Runtime 正在退出，禁止刷新会话记忆")
                self._task_cancellation = CancellationToken()
                self._task_active = True
                self._task_accepts_cancellation = True
            self._task_session_id = original.record.id
            refresh = getattr(original.agent, "refresh_review_memory", None)
            if not callable(refresh):
                raise SessionRuntimeError("当前 Agent 不支持会话记忆刷新")
            try:
                result = refresh(
                    original.context,
                    cancellation=self._task_cancellation,
                )
            except CancellationError as exc:
                raise SessionRuntimeError("会话记忆刷新已取消；原候选保持不变") from exc
            except MemorySummaryError as exc:
                label = memory_summary_failure_label(
                    memory_summary_failure_code(exc)
                )
                raise SessionRuntimeError(
                    f"会话记忆刷新失败（{label}）；原候选保持不变"
                ) from exc
            except MemoryValidationError as exc:
                raise SessionRuntimeError(
                    "会话记忆刷新结果无效；原候选保持不变"
                ) from exc
            if not isinstance(result, MemoryRefreshResult):
                raise SessionRuntimeError("会话记忆刷新结果类型无效")
            with self._task_state_lock:
                cancelled = bool(
                    self._task_cancellation is not None
                    and self._task_cancellation.is_cancelled
                )
                self._task_accepts_cancellation = False
            if cancelled:
                raise SessionRuntimeError("会话记忆刷新已取消；原候选保持不变")
            if (
                self.current.record.id != original.record.id
                or self.current.context != original.context
            ):
                raise SessionRuntimeError("会话状态在刷新期间已变化，拒绝提交候选")
            candidate = result.context.review_memory_candidate
            if not isinstance(candidate, ConversationMemory):
                raise SessionRuntimeError("会话记忆刷新未生成有效候选")
            self._require_complete_memory_candidate(result.context, candidate)
            self.current = replace(original, context=result.context)
            self._cache_current()
            return result
        finally:
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
                try:
                    if self._close_requested:
                        self._close_locked(retry_cleanup=False)
                finally:
                    with self._task_state_lock:
                        self._release_task_ownership_locked()

    def close(self) -> bool:
        """停止接收操作并释放 Session；活动任务负责收尾后释放，调用方不抢锁。"""
        with self._task_state_lock:
            self._shutdown_requested = True
            self._close_requested = True
        self.cancel_current()
        if not self._task_lock.acquire(blocking=False):
            return False
        try:
            return self._close_locked()
        finally:
            self._task_lock.release()

    def _close_locked(self, *, retry_cleanup: bool = True) -> bool:
        if self._closed:
            return self._close_ok
        if retry_cleanup:
            self._retry_pending_cleanup_locked()
        if self._pending_cleanup:
            return False
        self._close_ok = False
        try:
            self._close_ok = self._retry_persist_locked()
            return self._close_ok
        finally:
            self._closed = True
            self._session_cache.clear()
            self._workspace_baselines.clear()
            self._baseline_confirmation_required.clear()
            self._workspace_baseline_unresolved.clear()
            self._pending_undo_binding = None
            if self._session_lock is not None:
                self._session_lock.close()
                self._session_lock = None
            self._release_workspace_lock_if_safe()

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
            cleaned = self._retry_pending_cleanup_locked()
            if cleaned:
                self._release_workspace_lock_if_safe()
            return cleaned
        finally:
            self._task_lock.release()

    def _release_workspace_lock_if_safe(self) -> None:
        """只有受管资源均确认结束后才交出工作区所有权。"""

        if self._pending_cleanup or self._task_active or self._pending_undo_binding is not None:
            return
        ownership = self._workspace_lock
        if ownership is not None:
            ownership.clear_active()
            ownership.close()
            self._workspace_lock = None

    def _retry_pending_cleanup_locked(self) -> bool:
        # 一次退出重试共享新预算；旧 scope 的失败事实与 exact 资源身份仍然保留。
        deadline = time.monotonic() + 5.0
        self._pending_cleanup = [scope for scope in self._pending_cleanup
                                 if not scope.retry(deadline)]
        return not self._pending_cleanup

    def _observe_workspace_change(self, _preview: WorkspaceGatePreview) -> None:
        """检测即撤销旧版本验证；用户拒绝也不能恢复旧通过状态。"""

        if self.current is None:
            return
        self._invalidate_current_after_workspace_finish_failure()

    @staticmethod
    def _safe_workspace_path(path: str) -> str:
        output: list[str] = []
        for character in path:
            codepoint = ord(character)
            if (
                codepoint < 32
                or codepoint == 127
                or unicodedata.category(character) in {"Cc", "Cf"}
            ):
                if codepoint <= 0xFF:
                    output.append(f"\\x{codepoint:02x}")
                elif codepoint <= 0xFFFF:
                    output.append(f"\\u{codepoint:04x}")
                else:
                    output.append(f"\\U{codepoint:08x}")
            else:
                output.append(character)
        return "".join(output)

    def _with_workspace_change_notice(
        self,
        active: ActiveSession,
        preview: WorkspaceGatePreview,
    ) -> ActiveSession:
        paths = "、".join(self._safe_workspace_path(path) for path in preview.changed_paths)
        notice = (
            "本地可信状态：工作区已变化并经用户确认作为本任务起点。"
            f"变化路径：{paths or '（无可列出路径）'}。"
            "历史源码观察和实现状态可能过期，请重新读取并核验相关代码；"
            "既有用户目标与约束仍保留。该确认不批准后续工具、写入或命令。"
        )
        return replace(
            active,
            # ``WorkspaceGate`` 已在展示首个 changed 预览时同步调用
            # change_observer 完成验证失效；这里不能再次清空刚刚证明仍绑定
            # 当前版本的失败/通过证据。
            context=replace(active.context, workspace_change_notice=notice),
        )

    def _clear_workspace_change_notice(self) -> None:
        if self.current is None or not self.current.context.workspace_change_notice:
            return
        self.current = replace(
            self.current,
            context=replace(self.current.context, workspace_change_notice=""),
        )
        self._cache_current()

    def _finalize_workspace_task(
        self,
        active: ActiveSession,
        result: RunResult,
    ) -> RunResult:
        """收尾完整扫描；只吸收可证明归属当前任务的最终文件版本。"""

        session_id = active.record.id
        baseline = self._workspace_baselines.get(session_id)
        try:
            current = self._workspace_gate.capture_current(
                active.record.workspace,
                # 任务取消不能取消强制收尾扫描，否则每次取消都会把安全
                # 检查本身伪装成扫描失败。扫描仍受固定时间/大小上限约束。
                cancellation=None,
            )
        except WorkspaceScanError:
            self._workspace_baseline_unresolved.add(session_id)
            self._invalidate_current_after_workspace_finish_failure()
            self._clear_workspace_change_notice()
            return replace(
                self._with_stale_task_validation(result),
                ok=False,
                summary=f"{result.summary}；工作区收尾扫描失败，当前代码状态待重新确认",
                verification="待验证",
            )
        if baseline is None:
            self._workspace_baseline_unresolved.add(session_id)
            self._invalidate_current_after_workspace_finish_failure()
            self._clear_workspace_change_notice()
            return replace(
                self._with_stale_task_validation(result),
                ok=False,
                summary=f"{result.summary}；工作区收尾缺少起始基线",
                verification="待验证",
            )
        try:
            comparison = compare_baselines(baseline, current)
            framework_paths = self._framework_owned_workspace_paths(active)
            attributable = task_changes_match_baselines(
                baseline,
                current,
                self._task_sealed_change_set,
                framework_owned_paths=framework_paths,
            )
        except (TypeError, ValueError):
            self._workspace_baseline_unresolved.add(session_id)
            self._invalidate_current_after_workspace_finish_failure()
            self._clear_workspace_change_notice()
            return replace(
                self._with_stale_task_validation(result),
                ok=False,
                summary=f"{result.summary}；工作区根身份或快照范围在收尾时变化",
                verification="待验证",
            )
        if not comparison.changed and attributable:
            self._workspace_baselines[session_id] = current
            self._workspace_baseline_unresolved.discard(session_id)
            self._clear_workspace_change_notice()
            return result
        change_set = self._task_sealed_change_set
        tainted = True
        if change_set is not None:
            tainted = bool(change_set.tainted_paths)
        elif attributable and framework_paths:
            # Provider 失败等无源码效果的任务可以吸收本框架自己的审计追加，
            # 但不能借此接受任何任务文件或未知变化。
            self._workspace_baselines[session_id] = current
            self._workspace_baseline_unresolved.discard(session_id)
            self._clear_workspace_change_notice()
            return result
        if (
            result.ok
            and attributable
            and not tainted
            and not result.unknown_effects
            and not result.cleanup_failed
        ):
            self._workspace_baselines[session_id] = current
            self._workspace_baseline_unresolved.discard(session_id)
            self._clear_workspace_change_notice()
            return result
        self._workspace_baseline_unresolved.add(session_id)
        self._invalidate_current_after_workspace_finish_failure()
        self._clear_workspace_change_notice()
        return replace(
            self._with_stale_task_validation(result),
            ok=False,
            summary=f"{result.summary}；收尾发现未归属的工作区变化，需下次任务前确认",
            verification="待验证",
        )

    @staticmethod
    def _with_stale_task_validation(result: RunResult) -> RunResult:
        """收尾无法证明 Agent 所见版本仍有效时，使命令检查同步过期。"""

        report = result.task_validation
        limitation = "工作区收尾状态与命令检查快照不一致"
        limitations = (
            report.limitations
            if limitation in report.limitations
            else (*report.limitations, limitation)
        )
        return replace(
            result,
            task_validation=replace(
                report,
                status="stale" if report.records else report.status,
                limitations=limitations,
            ),
        )

    @staticmethod
    def _framework_owned_workspace_paths(active: ActiveSession) -> tuple[str, ...]:
        """返回本 Session 在工作区内的精确审计文件；不泛化排除目录。"""

        audit = active.audit
        if audit is None:
            return ()
        audit_path = getattr(audit, "path", None)
        if not isinstance(audit_path, Path):
            return ()
        try:
            relative = audit_path.resolve(strict=False).relative_to(
                active.record.workspace.resolve(strict=True)
            )
        except (OSError, ValueError):
            return ()
        return (relative.as_posix(),)

    def _finalize_workspace_exception(self, active: ActiveSession) -> None:
        """保留主异常，同时尽力确认异常任务没有留下未接受的代码版本。"""

        session_id = active.record.id
        baseline = self._workspace_baselines.get(session_id)
        try:
            current = self._workspace_gate.capture_current(
                active.record.workspace,
                cancellation=None,
            )
        except (WorkspaceScanError, OSError, ValueError):
            self._workspace_baseline_unresolved.add(session_id)
            self._invalidate_current_after_workspace_finish_failure()
            return
        if baseline is not None:
            try:
                framework_only = task_changes_match_baselines(
                    baseline,
                    current,
                    None,
                    framework_owned_paths=self._framework_owned_workspace_paths(active),
                )
            except (TypeError, ValueError):
                framework_only = False
            if framework_only:
                self._workspace_baselines[session_id] = current
                self._workspace_baseline_unresolved.discard(session_id)
                return
        try:
            changed = baseline is None or compare_baselines(baseline, current).changed
        except (TypeError, ValueError):
            changed = True
        if changed:
            # 异常、取消或失败产生的部分效果必须在下次任务前重新确认；
            # 即使变更恰好出现在工具账本中，也不能在没有正常收尾时自动接受。
            self._workspace_baseline_unresolved.add(session_id)
            self._invalidate_current_after_workspace_finish_failure()

    def _invalidate_current_after_workspace_finish_failure(self) -> None:
        if self.current is None:
            return
        original = self.current
        failure = original.context.verification_failure
        evidence = original.context.verification_evidence
        keep_failure = False
        keep_pass = False
        scope = getattr(getattr(original.tools, "context", None), "verification_scope", None)
        policy = getattr(getattr(original.tools, "context", None), "workspace_policy", None)
        if isinstance(scope, VerificationScope) and policy is not None:
            try:
                current_snapshot = scope.capture(policy)
                keep_failure = failure is not None and stable_snapshots(
                    failure, current_snapshot
                )
                keep_pass = (
                    evidence is not None
                    and scope.owns(evidence)
                    and evidence.is_valid_for(current_snapshot)
                )
            except (OSError, ValueError, TypeError):
                keep_failure = False
                keep_pass = False
        if keep_failure:
            self.current = replace(
                original,
                context=replace(
                    original.context,
                    verification="失败",
                    verification_failure=failure,
                    verification_required=True,
                ),
                memory=replace(original.memory, verification="failed"),
            )
        elif keep_pass:
            self.current = replace(
                original,
                context=replace(original.context, verification="通过"),
                memory=replace(original.memory, verification="passed"),
            )
        else:
            self.current = self._invalidate_verification(original, force=True)
        self._cache_current()
        self._memory_dirty = self.current.memory != self._persisted_memory

    def _run_task_locked(self, task: str) -> RunResult:
        original = self.current
        if original.memory.unknown_effects or original.context.unknown_effects:
            return RunResult(
                False, "文件影响未确认；请检查实际文件并通过 /clear 明确确认", 0,
                modified_files=original.context.modified_files, verification="待验证",
                unknown_effects=True,
                modified_directories=original.context.modified_directories,
            )
        original.journal.begin_task(
            tuple(original.context.modified_files),
            original.context.verification,
        )
        original.journal.set_directory_baseline(
            tuple(original.context.modified_directories)
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
                self._task_sealed_change_set = original.journal.seal_task(
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
            modified_directories=reconciled.modified_directories,
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
            self._task_sealed_change_set = original.journal.seal_task(
                reconciled.modified_files,
                reconciled.verification,
            )
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
        if effects.state is EffectState.NONE:
            return context
        return apply_tool_transition(context, effects)

    @staticmethod
    def _invalidate_verification(active: ActiveSession, *, force: bool = False) -> ActiveSession:
        """切会话/撤销只撤销本地能力，不把恢复旧内容解释为恢复旧证明。"""
        context = active.context
        required = (force or context.verification_required or bool(context.modified_files)
                    or context.verification_evidence is not None
                    or context.verification_failure is not None
                    or _normalized_verification(context.verification) in {"passed", "failed", "待验证"})
        scope = getattr(getattr(active.tools, "context", None), "verification_scope", None)
        if isinstance(scope, VerificationScope):
            # 只轮换能力 authority，保留同一工作区验证 scope；旧 evidence
            # 立即失去所有权，新任务仍可在相同覆盖范围签发新证据。
            scope.revoke()
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

        latest = self._require_active_session().journal.latest()
        if latest is None:
            return None
        if latest.tainted_paths or latest.tainted_directory_paths:
            if latest.tainted_directory_paths:
                message = (
                    "无法显示，任务文件或目录状态冲突："
                    f"{'、'.join((*latest.tainted_paths, *latest.tainted_directory_paths))}"
                )
            else:
                message = (
                    f"无法显示，任务文件状态冲突：{'、'.join(latest.tainted_paths)}"
                )
            raise SessionRuntimeError(message)
        return render_change_set_diff(latest)

    @_idle_runtime_change
    def prepare_undo(self) -> UndoPreview:
        """在工作区锁内生成撤销预览，并绑定完整代码快照直到确认结束。"""

        active = self._require_active_session()
        change_set, tools = self._undo_inputs()
        paths = tuple(
            change.path
            for change in (*change_set.changes, *change_set.directory_changes)
        )
        try:
            self._workspace_lock = WorkspaceLock.acquire(active.record.workspace)
        except WorkspaceLockBusyError as exc:
            raise SessionRuntimeError("工作区正在执行其他任务") from exc
        except WorkspaceRecoveryRequiredError as exc:
            raise SessionRuntimeError("检测到上次任务未确认清理，无法预览撤销") from exc
        except (WorkspaceIdentityError, WorkspaceLockError, OSError) as exc:
            raise SessionRuntimeError("无法安全取得撤销所需的工作区锁") from exc
        self._workspace_gate_generation += 1
        try:
            gate = self._workspace_gate.enter(
                active.record.workspace,
                baseline=self._workspace_baselines.get(active.record.id),
                require_initialization_confirmation=(
                    active.record.id in self._baseline_confirmation_required
                ),
                session_id=active.record.id,
                request_id=secrets.token_hex(16),
                generation=self._workspace_gate_generation,
                confirmer=self._workspace_confirmer,
                cancellation=None,
                change_observer=self._observe_workspace_change,
            )
            self._workspace_baselines[active.record.id] = gate.baseline
            self._baseline_confirmation_required.discard(active.record.id)
            preview = tools.preview_undo(change_set)
            bound = self._workspace_gate.capture_current(
                active.record.workspace,
                cancellation=None,
            )
            if bound.snapshot_id != gate.baseline.snapshot_id:
                raise SessionRuntimeError("工作区在撤销预览期间发生变化，请重新预览")
            self._pending_undo_binding = _WorkspaceUndoBinding(
                session_id=active.record.id,
                generation=self._workspace_gate_generation,
                snapshot_id=bound.snapshot_id,
                baseline=bound,
                change_set=change_set,
            )
            return preview
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
        except WorkspaceScanError as exc:
            raise SessionRuntimeError(
                f"工作区扫描失败（{exc.reason}），撤销未开始"
            ) from exc
        except WorkspaceConfirmationUnavailable as exc:
            raise SessionRuntimeError("当前入口无法确认工作区变化，撤销未开始") from exc
        except WorkspaceConfirmationRejected as exc:
            raise SessionRuntimeError("已拒绝工作区变化，撤销未开始") from exc
        except (WorkspaceGateCancelled, WorkspaceGateError) as exc:
            raise SessionRuntimeError(str(exc)) from exc
        except (PolicyError, OSError, UnicodeError, ValueError) as exc:
            self._audit_undo(
                status="failed",
                paths=paths,
                compensation_status="not-required",
            )
            raise SessionRuntimeError("无法安全预览最近任务的撤销") from exc
        finally:
            if self._pending_undo_binding is None:
                self._release_undo_workspace()

    @_idle_runtime_change
    def undo_latest(self) -> UndoExecution:
        """只提交与预览所绑定代码版本一致的撤销，并在收尾后释放锁。"""

        active = self._require_active_session()
        binding = self._pending_undo_binding
        if binding is None or self._workspace_lock is None:
            raise SessionRuntimeError("撤销预览不存在或已失效，请重新执行 /undo")
        if binding.session_id != active.record.id:
            self._release_undo_workspace()
            raise SessionRuntimeError("撤销预览不属于当前 Session，请重新预览")
        change_set, tools = self._undo_inputs()
        paths = tuple(
            sorted(
                change.path
                for change in (*change_set.changes, *change_set.directory_changes)
            )
        )
        try:
            candidate = self._workspace_gate.capture_current(
                active.record.workspace,
                cancellation=None,
            )
        except WorkspaceScanError as exc:
            self._release_undo_workspace()
            raise SessionRuntimeError(
                f"工作区扫描失败（{exc.reason}），撤销未执行"
            ) from exc
        if candidate.snapshot_id != binding.snapshot_id:
            self._release_undo_workspace()
            raise SessionRuntimeError("工作区在撤销确认后发生变化，请重新预览")
        try:
            self._workspace_lock.mark_active()
        except (WorkspaceRecoveryRequiredError, WorkspaceIdentityError, WorkspaceLockError) as exc:
            self._release_undo_workspace()
            raise SessionRuntimeError("无法安全建立撤销活动标记") from exc

        try:
            execution = tools.undo_change_set(change_set)
        except (PolicyError, OSError, UnicodeError, ValueError, TypeError):
            self._audit_undo(
                status="failed",
                paths=paths,
                compensation_status="not-required",
            )
            self._finish_undo_workspace(binding, accepted=False)
            raise SessionRuntimeError("无法安全执行最近任务的撤销") from None
        except BaseException:
            self._finish_undo_workspace(binding, accepted=False)
            raise
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
            self._finish_undo_workspace(
                binding,
                accepted=False,
                compensated_change_set=execution._compensated_change_set,
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
            modified_directories=change_set.before_modified_directories,
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
        if not self._finish_undo_workspace(
            binding,
            accepted=True,
            restored_changes=execution._restored_changes,
            restored_directory_changes=execution._restored_directory_changes,
        ):
            raise SessionRuntimeError("撤销已执行，但工作区收尾扫描或清理失败")
        return execution

    @_idle_runtime_change
    def cancel_undo(self) -> None:
        """明确拒绝撤销时丢弃绑定预览，并释放尚未开始执行的工作区锁。"""

        self._release_undo_workspace()

    def _finish_undo_workspace(
        self,
        binding: _WorkspaceUndoBinding,
        *,
        accepted: bool,
        restored_changes: tuple[FileChange, ...] = (),
        restored_directory_changes: tuple[DirectoryChange, ...] = (),
        compensated_change_set: TaskChangeSet | None = None,
    ) -> bool:
        """撤销收尾复扫；失败或部分效果绝不更新可信基线。"""

        complete = True
        try:
            current = self._workspace_gate.capture_current(
                self._require_active_session().record.workspace,
                cancellation=None,
            )
        except WorkspaceScanError:
            complete = False
            self._workspace_baseline_unresolved.add(binding.session_id)
            self._invalidate_current_after_workspace_finish_failure()
        else:
            try:
                changed = compare_baselines(binding.baseline, current).changed
                originals = {
                    change.path: change
                    for change in binding.change_set.changes
                }
                restored = {
                    change.path: change
                    for change in restored_changes
                }
                original_directories = {
                    change.path: change
                    for change in binding.change_set.directory_changes
                }
                restored_directories = {
                    change.path: change
                    for change in restored_directory_changes
                }
                if compensated_change_set is not None:
                    compensated_files = {
                        change.path: change
                        for change in compensated_change_set.changes
                    }
                    compensated_directories = {
                        change.path: change
                        for change in compensated_change_set.directory_changes
                    }
                    if (
                        len(compensated_files)
                        != len(compensated_change_set.changes)
                        or set(compensated_files) != set(originals)
                        or any(
                            compensated_files[path].before != original.before
                            for path, original in originals.items()
                        )
                        or len(compensated_directories)
                        != len(compensated_change_set.directory_changes)
                        or set(compensated_directories)
                        != set(original_directories)
                        or any(
                            compensated_directories[path].before != original.before
                            for path, original in original_directories.items()
                        )
                    ):
                        raise ValueError("撤销补偿证据不完整或不匹配")
                    reversed_change_set = replace(
                        binding.change_set,
                        changes=tuple(
                            FileChange(
                                path,
                                originals[path].after,
                                compensated_files[path].after,
                            )
                            for path in sorted(originals)
                        ),
                        directory_changes=tuple(
                            DirectoryChange(
                                path,
                                original_directories[path].after,
                                compensated_directories[path].after,
                            )
                            for path in sorted(original_directories)
                        ),
                        tainted_paths=(),
                        tainted_directory_paths=(),
                    )
                else:
                    if (
                        len(restored) != len(restored_changes)
                        or set(restored) != set(originals)
                        or any(
                            restored[path].before != original.after
                            for path, original in originals.items()
                        )
                        or len(restored_directories)
                        != len(restored_directory_changes)
                        or set(restored_directories) != set(original_directories)
                        or any(
                            restored_directories[path].before != original.after
                            for path, original in original_directories.items()
                        )
                    ):
                        raise ValueError("撤销恢复证据不完整或不匹配")
                    reversed_change_set = replace(
                        binding.change_set,
                        changes=tuple(
                            restored[path] for path in sorted(restored)
                        ),
                        directory_changes=tuple(
                            restored_directories[path]
                            for path in sorted(restored_directories)
                        ),
                        tainted_paths=(),
                        tainted_directory_paths=(),
                    )
                attributable = task_changes_match_baselines(
                    binding.baseline,
                    current,
                    reversed_change_set,
                    framework_owned_paths=self._framework_owned_workspace_paths(
                        self._require_active_session()
                    ),
                )
            except (TypeError, ValueError):
                changed = True
                attributable = False
            if compensated_change_set is not None and attributable:
                try:
                    self.current.journal.replace_latest(
                        binding.change_set,
                        compensated_change_set,
                    )
                except Exception:
                    complete = False
                    self._workspace_baseline_unresolved.add(binding.session_id)
                    self._invalidate_current_after_workspace_finish_failure()
                else:
                    # 补偿重建会改变目录/文件 identity；即使源码正文相同，
                    # 旧验证证据绑定的工作区版本也已失效，必须立即撤销 authority。
                    self.current = self._invalidate_verification(
                        self.current,
                        force=True,
                    )
                    self._cache_current()
                    self._memory_dirty = (
                        self.current.memory != self._persisted_memory
                    )
                    if not self._persist_current():
                        complete = False
                    self._workspace_baselines[binding.session_id] = current
                    self._workspace_baseline_unresolved.discard(binding.session_id)
            elif compensated_change_set is not None:
                complete = False
                self._workspace_baseline_unresolved.add(binding.session_id)
                self._invalidate_current_after_workspace_finish_failure()
            elif accepted and attributable:
                self._workspace_baselines[binding.session_id] = current
                self._workspace_baseline_unresolved.discard(binding.session_id)
            elif accepted:
                complete = False
                self._workspace_baseline_unresolved.add(binding.session_id)
                self._invalidate_current_after_workspace_finish_failure()
            elif changed:
                complete = False
                self._workspace_baseline_unresolved.add(binding.session_id)
                self._invalidate_current_after_workspace_finish_failure()
            else:
                self._workspace_baselines[binding.session_id] = current
        try:
            self._release_undo_workspace()
        except (WorkspaceIdentityError, WorkspaceLockError, OSError):
            complete = False
        return complete

    def _release_undo_workspace(self) -> None:
        self._pending_undo_binding = None
        ownership = self._workspace_lock
        if ownership is None:
            return
        try:
            ownership.clear_active()
        finally:
            ownership.close()
            self._workspace_lock = None

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
        if self.current is None:
            return True
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
        return self._retry_persist_locked()

    def _retry_persist_locked(self) -> bool:
        if self.current is None:
            return True
        legacy_ok = self._persist_current()
        context = self.current.context
        if not context.memory_pending_clear:
            return legacy_ok
        try:
            self.store.clear_conversation_memory(self.current.record.id)
        except (SessionError, OSError):
            self._unsaved_memory = True
            self._warning = "会话记忆持久化清除失败；内存已清空，可本地重试"
            return False
        self.current = replace(
            self.current,
            context=replace(
                context,
                persisted_memory_revision=None,
                memory_pending_clear=False,
            ),
        )
        self._cache_current()
        if legacy_ok:
            self._clear_unsaved_warning()
        return legacy_ok

    @property
    def permission_level(self) -> str:
        """当前会话的权限级别：strict（默认）、relaxed 或 fullaccess。"""
        if self.current is None:
            return self._entry_permission
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
        if self.current is None:
            self._entry_permission = normalized
            return normalized
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
        if self.current is None:
            previews = self.preview_models()
            model = previews.get(self._entry_provider, self._entry_model or "")
            return RuntimeStatus(
                None,
                False,
                self._warning,
                workspace=self._entry_workspace,
                provider=self._entry_provider,
                model=model,
                read_only=self.options.read_only,
                permission_level=self._entry_permission,
            )
        pending_clear = self.current.context.memory_pending_clear
        return RuntimeStatus(
            self.current.record,
            self._unsaved_memory or pending_clear,
            self._warning,
            workspace=self.current.record.workspace,
            provider=self.current.record.provider,
            model=self.current.record.model,
            read_only=self.current.config.read_only,
            permission_level=self.current.memory.permission_level,
            verification=self.current.memory.verification,
            context_messages=len(self.current.context.messages),
            modified_files=len(self.current.memory.modified_files),
            modified_directories=len(
                self.current.context.modified_directories
            ),
        )

    def render_memory(self) -> str:
        """本地显示当前语义记忆及保存状态，不写审计。"""

        context = self.current.context
        memory = context.conversation_memory
        if not isinstance(memory, ConversationMemory):
            raise SessionRuntimeError("会话记忆状态无效")
        review_candidate = context.review_memory_candidate
        if review_candidate is not None and not isinstance(
            review_candidate, ConversationMemory
        ):
            raise SessionRuntimeError("会话记忆保存候选状态无效")
        displayed = review_candidate or memory
        saved = (
            "未保存"
            if context.persisted_memory_revision is None
            else f"已保存 revision {context.persisted_memory_revision}"
        )
        pending = "；持久化清除待重试" if context.memory_pending_clear else ""
        display_label = "待保存候选" if review_candidate is not None else "运行时记忆"
        target = context.latest_completed_task_seq
        if displayed.covered_through == target:
            coverage = f"覆盖状态：完整（消息 {displayed.covered_through}）"
        elif displayed.covered_through < target:
            coverage = (
                "候选覆盖不足："
                f"当前到消息 {displayed.covered_through}，目标到消息 {target}"
            )
        else:
            coverage = (
                "覆盖边界无效："
                f"当前到消息 {displayed.covered_through}，目标到消息 {target}"
            )
        return (
            f"会话记忆（显示：{display_label} revision {displayed.revision}；"
            f"运行时 revision {memory.revision}；{saved}{pending}；{coverage}）\n"
            f"{memory_to_json(displayed)}"
        )

    def render_memory_archive(self) -> str:
        """列出当前目标归档的必要元信息和容量，不显示条目正文。"""

        context = self.current.context
        memory = context.conversation_memory
        review_candidate = context.review_memory_candidate
        if not isinstance(memory, ConversationMemory) or (
            review_candidate is not None
            and not isinstance(review_candidate, ConversationMemory)
        ):
            raise SessionRuntimeError("会话记忆状态无效")
        displayed = review_candidate or memory
        target = "待保存候选" if review_candidate is not None else "运行时记忆"
        encoded_chars = len(memory_to_json(displayed))
        limit = self.current.config.memory.summary_max_chars
        lines = [
            f"归档目标：{target}",
            f"归档容量：{len(displayed.archived)}/{MAX_ARCHIVED_ITEMS}；"
            f"字符容量：{encoded_chars}/{limit}",
        ]
        if not displayed.archived:
            lines.append("当前没有归档条目")
        else:
            lines.extend(
                f"- {entry.item.id} | {entry.section} | {entry.item.state}"
                for entry in displayed.archived
            )
        return "\n".join(lines)

    @staticmethod
    def _require_complete_memory_candidate(
        context: SessionContext,
        candidate: ConversationMemory,
    ) -> None:
        """保存只能覆盖到 Agent 已确认成功结束的最新任务水位。"""

        target = context.latest_completed_task_seq
        if candidate.covered_through < target:
            raise SessionRuntimeError(
                "会话记忆候选覆盖不足："
                f"当前覆盖到消息 {candidate.covered_through}，"
                f"最新已完成任务到消息 {target}；请先运行 /memory refresh"
            )
        if candidate.covered_through > target:
            raise SessionRuntimeError("会话记忆候选覆盖边界无效，请重新刷新")

    @_idle_runtime_change
    def preview_memory_save(self) -> MemorySavePreview:
        """返回确切候选和位置；敏感标记只导致固定错误，不回显正文。"""

        context = self.current.context
        if self.current.config.memory.persistence != "reviewed_summary":
            raise SessionRuntimeError("会话记忆持久化未启用")
        if context.memory_pending_clear:
            raise SessionRuntimeError("持久化清除尚未完成，禁止保存旧记忆")
        memory = context.conversation_memory
        if not isinstance(memory, ConversationMemory):
            raise SessionRuntimeError("会话记忆状态无效")
        review_candidate = context.review_memory_candidate
        if review_candidate is not None and not isinstance(
            review_candidate, ConversationMemory
        ):
            raise SessionRuntimeError("会话记忆保存候选状态无效")
        candidate = review_candidate or memory
        self._require_complete_memory_candidate(context, candidate)
        encoded = memory_to_json(candidate)
        if _SENSITIVE_MEMORY_PATTERN.search(encoded):
            raise SessionRuntimeError("候选可能包含敏感或私有内容；请先本地编辑或拒绝保存")
        text = (
            f"保存位置：{self.store.database_path}\n"
            f"覆盖状态：完整；候选到消息 {candidate.covered_through}；"
            f"最新已完成任务到消息 {context.latest_completed_task_seq}\n"
            f"将保存字段：目标、约束、决策、待办、来源、scope、revision；"
            f"不保存源码正文或原始工具输出。\n{encoded}"
        )
        return MemorySavePreview(
            session_id=self.current.record.id,
            generation=memory.generation,
            revision=memory.revision,
            persisted_revision=context.persisted_memory_revision,
            next_message_seq=context.next_message_seq,
            text=text,
            original_memory=memory,
            original_review_candidate=review_candidate,
            candidate=candidate,
        )

    @_idle_runtime_change
    def save_memory_preview(self, preview: MemorySavePreview) -> None:
        """只保存仍与预览完全相同的候选；数据库失败时保留内存候选。"""

        if not isinstance(preview, MemorySavePreview):
            raise SessionRuntimeError("记忆保存预览无效")
        context = self.current.context
        if self.current.config.memory.persistence != "reviewed_summary":
            raise SessionRuntimeError("会话记忆持久化未启用")
        if context.memory_pending_clear:
            raise SessionRuntimeError("持久化清除尚未完成，禁止保存旧记忆")
        memory = context.conversation_memory
        current_candidate = context.review_memory_candidate or memory
        if (
            not isinstance(memory, ConversationMemory)
            or self.current.record.id != preview.session_id
            or memory != preview.original_memory
            or memory.generation != preview.generation
            or memory.revision != preview.revision
            or context.review_memory_candidate != preview.original_review_candidate
            or preview.candidate != current_candidate
            or context.persisted_memory_revision != preview.persisted_revision
            or context.next_message_seq != preview.next_message_seq
        ):
            raise SessionRuntimeError("候选在确认期间已变化，请重新预览")
        self._require_complete_memory_candidate(context, current_candidate)
        allowed_sources = memory_source_ids(memory) | memory_source_ids(current_candidate)
        allowed_sources.update(
            source_id_for_sequence(message.message_seq)
            for message in context.messages
            if message.message_seq is not None
        )
        try:
            validate_candidate(
                preview.candidate,
                allowed_source_ids=allowed_sources,
                expected_generation=preview.generation,
                max_chars=self.current.config.memory.summary_max_chars,
            )
            encoded = memory_to_json(preview.candidate)
        except MemoryValidationError as exc:
            raise SessionRuntimeError("会话记忆保存候选无效") from exc
        if _SENSITIVE_MEMORY_PATTERN.search(encoded):
            raise SessionRuntimeError("候选可能包含敏感或私有内容；请重新预览")
        try:
            saved_revision = self.store.save_conversation_memory(
                preview.session_id,
                preview.candidate,
                preview.next_message_seq,
                preview.persisted_revision,
            )
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("会话记忆未保存，可直接重试；业务工具不会重跑") from exc
        self.current = replace(
            self.current,
            context=replace(context, persisted_memory_revision=saved_revision),
        )
        self._cache_current()

    @_idle_runtime_change
    def preview_memory_edit(
        self,
        item_id: str,
        text: str,
        scope: str,
        state: str | None = None,
    ) -> MemoryEditPreview:
        """生成本地编辑候选；空文本表示删除，确认前不修改任何状态。"""

        context = self.current.context
        if context.memory_pending_clear:
            raise SessionRuntimeError("持久化清除尚未完成，禁止编辑旧记忆")
        memory = context.conversation_memory
        if not isinstance(memory, ConversationMemory):
            raise SessionRuntimeError("会话记忆状态无效")
        review_candidate = context.review_memory_candidate
        if review_candidate is not None and not isinstance(
            review_candidate, ConversationMemory
        ):
            raise SessionRuntimeError("会话记忆保存候选状态无效")
        base = review_candidate or memory
        if not isinstance(item_id, str) or not item_id.strip():
            raise SessionRuntimeError("记忆条目 ID 不能为空")
        if not isinstance(text, str) or len(text) > 500:
            raise SessionRuntimeError("记忆条目文本不能超过 500 字符")
        if scope not in {"task", "session"}:
            raise SessionRuntimeError("记忆 scope 只能是 task 或 session")
        if state is not None and state not in {
            "active", "pending", "done", "cancelled", "superseded",
        }:
            raise SessionRuntimeError("记忆 state 无效")
        source_id = source_id_for_sequence(context.next_message_seq)
        candidate = self._edited_memory(
            base, item_id.strip(), text, scope, source_id, state
        )
        try:
            validate_candidate(
                candidate,
                allowed_source_ids=memory_source_ids(base) | {source_id},
                max_chars=self.current.config.memory.summary_max_chars,
            )
        except MemoryValidationError as exc:
            raise SessionRuntimeError("记忆编辑候选无效") from exc
        action = "删除" if not text.strip() else "更新"
        state_text = state or "保持原状态"
        preview_text = (
            f"{action}条目 {item_id.strip()}；scope={scope}；state={state_text}；"
            f"确认时绑定 revision {base.revision}。\n{memory_to_json(candidate)}"
        )
        return MemoryEditPreview(
            session_id=self.current.record.id,
            generation=base.generation,
            revision=base.revision,
            next_message_seq=context.next_message_seq,
            text=preview_text,
            original_memory=memory,
            original_review_candidate=review_candidate,
            targets_review_candidate=review_candidate is not None,
            candidate=candidate,
        )

    @_idle_runtime_change
    def apply_memory_edit(self, preview: MemoryEditPreview) -> None:
        """确认后原子应用编辑并追加不含编辑正文的本地来源消息。"""

        if not isinstance(preview, MemoryEditPreview):
            raise SessionRuntimeError("记忆编辑预览无效")
        context = self.current.context
        memory = context.conversation_memory
        review_candidate = context.review_memory_candidate
        base = review_candidate or memory
        if (
            not isinstance(memory, ConversationMemory)
            or not isinstance(base, ConversationMemory)
            or self.current.record.id != preview.session_id
            or memory != preview.original_memory
            or review_candidate != preview.original_review_candidate
            or base.generation != preview.generation
            or base.revision != preview.revision
            or (review_candidate is not None) != preview.targets_review_candidate
            or context.next_message_seq != preview.next_message_seq
            or context.memory_pending_clear
        ):
            raise SessionRuntimeError("记忆在确认期间已变化，请重新预览")
        source_id = source_id_for_sequence(preview.next_message_seq)
        try:
            validate_candidate(
                preview.candidate,
                allowed_source_ids=memory_source_ids(base) | {source_id},
                expected_generation=preview.generation,
                max_chars=self.current.config.memory.summary_max_chars,
            )
        except MemoryValidationError as exc:
            raise SessionRuntimeError("记忆编辑候选无效") from exc
        if (
            preview.candidate.revision != preview.revision + 1
            or preview.candidate.covered_through != base.covered_through
        ):
            raise SessionRuntimeError("记忆编辑候选无效")
        source_message = Message(
            "user",
            "用户已确认本地记忆编辑",
            kind="memory_edit",
            message_seq=preview.next_message_seq,
        )
        next_context = replace(
            context,
            messages=(*context.messages, source_message),
            next_message_seq=preview.next_message_seq + 1,
        )
        if preview.targets_review_candidate:
            next_context = replace(
                next_context,
                review_memory_candidate=preview.candidate,
            )
        else:
            next_context = replace(
                next_context,
                conversation_memory=preview.candidate,
            )
        self.current = replace(self.current, context=next_context)
        self._cache_current()

    @_idle_runtime_change
    def preview_memory_archive_delete(
        self,
        item_id: str,
    ) -> MemoryArchiveDeletePreview:
        """生成归档逐项删除预览；确认前不修改内存或数据库。"""

        context = self.current.context
        if context.memory_pending_clear:
            raise SessionRuntimeError("持久化清除尚未完成，禁止整理旧记忆")
        memory = context.conversation_memory
        review_candidate = context.review_memory_candidate
        if not isinstance(memory, ConversationMemory) or (
            review_candidate is not None
            and not isinstance(review_candidate, ConversationMemory)
        ):
            raise SessionRuntimeError("会话记忆状态无效")
        normalized_id = item_id.strip() if isinstance(item_id, str) else ""
        if not normalized_id:
            raise SessionRuntimeError("归档条目 ID 不能为空")
        base = review_candidate or memory
        matches = [
            (index, entry)
            for index, entry in enumerate(base.archived)
            if entry.item.id == normalized_id
        ]
        if len(matches) != 1:
            raise SessionRuntimeError("找不到唯一的归档记忆条目")
        index, archived_item = matches[0]
        remaining = (*base.archived[:index], *base.archived[index + 1 :])
        candidate = replace(
            base,
            revision=base.revision + 1,
            archived=remaining,
        )
        try:
            validate_candidate(
                candidate,
                allowed_source_ids=memory_source_ids(base),
                expected_generation=base.generation,
                max_chars=self.current.config.memory.summary_max_chars,
            )
        except MemoryValidationError as exc:
            raise SessionRuntimeError("归档删除候选无效") from exc
        target = "待保存候选" if review_candidate is not None else "运行时记忆"
        after_chars = len(memory_to_json(candidate))
        text = (
            f"删除归档条目 {normalized_id}；类别={archived_item.section}；"
            f"state={archived_item.item.state}；目标={target}。\n"
            f"删除后容量：归档 {len(candidate.archived)}/{MAX_ARCHIVED_ITEMS}；"
            f"字符容量 {after_chars}/{self.current.config.memory.summary_max_chars}。\n"
            "确认只更新当前内存候选，不立即改写数据库；"
            "如需跨重启生效，请再次执行 /memory save。"
        )
        return MemoryArchiveDeletePreview(
            session_id=self.current.record.id,
            generation=base.generation,
            revision=base.revision,
            next_message_seq=context.next_message_seq,
            text=text,
            original_memory=memory,
            original_review_candidate=review_candidate,
            targets_review_candidate=review_candidate is not None,
            archived_item=archived_item,
            candidate=candidate,
        )

    @_idle_runtime_change
    def apply_memory_archive_delete(
        self,
        preview: MemoryArchiveDeletePreview,
    ) -> None:
        """确认后删除仍与预览完全一致的一条归档；不自动保存数据库。"""

        if not isinstance(preview, MemoryArchiveDeletePreview):
            raise SessionRuntimeError("归档删除预览无效")
        context = self.current.context
        memory = context.conversation_memory
        review_candidate = context.review_memory_candidate
        base = review_candidate or memory
        if (
            not isinstance(memory, ConversationMemory)
            or not isinstance(base, ConversationMemory)
            or self.current.record.id != preview.session_id
            or memory != preview.original_memory
            or review_candidate != preview.original_review_candidate
            or base.generation != preview.generation
            or base.revision != preview.revision
            or (review_candidate is not None) != preview.targets_review_candidate
            or context.next_message_seq != preview.next_message_seq
            or context.memory_pending_clear
        ):
            raise SessionRuntimeError("记忆在确认期间已变化，请重新预览")
        try:
            index = base.archived.index(preview.archived_item)
        except ValueError as exc:
            raise SessionRuntimeError("归档条目在确认期间已变化，请重新预览") from exc
        expected = replace(
            base,
            revision=base.revision + 1,
            archived=(*base.archived[:index], *base.archived[index + 1 :]),
        )
        if preview.candidate != expected:
            raise SessionRuntimeError("归档删除候选无效，请重新预览")
        try:
            validate_candidate(
                preview.candidate,
                allowed_source_ids=memory_source_ids(base),
                expected_generation=preview.generation,
                max_chars=self.current.config.memory.summary_max_chars,
            )
        except MemoryValidationError as exc:
            raise SessionRuntimeError("归档删除候选无效") from exc
        next_context = (
            replace(context, review_memory_candidate=preview.candidate)
            if preview.targets_review_candidate
            else replace(context, conversation_memory=preview.candidate)
        )
        self.current = replace(self.current, context=next_context)
        self._cache_current()

    @staticmethod
    def _edited_memory(
        memory: ConversationMemory,
        item_id: str,
        text: str,
        scope: str,
        source_id: str,
        state: str | None,
    ) -> ConversationMemory:
        sections: dict[str, list[MemoryItem]] = {
            "constraints": list(memory.constraints),
            "decisions": list(memory.decisions),
            "open_items": list(memory.open_items),
        }
        goal = memory.goal
        found: tuple[str, int, MemoryItem] | None = None
        if goal is not None and goal.id == item_id:
            found = ("goal", 0, goal)
        for section, entries in sections.items():
            for index, entry in enumerate(entries):
                if entry.id == item_id:
                    found = (section, index, entry)
        if found is None:
            raise SessionRuntimeError("找不到记忆条目")
        section, index, old = found
        archived = list(memory.archived)
        if not text.strip():
            if section == "goal":
                goal = None
            else:
                del sections[section][index]
        else:
            task_id = None if scope == "session" else (old.task_id or f"task-memory-{source_id[1:]}")
            next_state = old.state if state is None else state
            allowed_states = {
                "goal": {"active", "superseded"},
                "constraints": {"active"},
                "decisions": {"active", "superseded"},
                "open_items": {"pending", "done", "cancelled"},
            }[section]
            if next_state not in allowed_states:
                raise SessionRuntimeError("所选 state 不适用于该记忆类别")
            updated = MemoryItem(
                old.id,
                text.strip(),
                (source_id,),
                scope,
                task_id,
                state=next_state,
                replaces_id=old.replaces_id if next_state == "active" else None,
            )
            terminal = next_state in {"done", "cancelled", "superseded"}
            if terminal:
                archived.append(ArchivedMemoryItem(section, updated))
                if section == "goal":
                    goal = None
                else:
                    del sections[section][index]
            elif section == "goal":
                goal = updated
            else:
                sections[section][index] = updated
        return ConversationMemory(
            revision=memory.revision + 1,
            generation=memory.generation,
            covered_through=memory.covered_through,
            goal=goal,
            constraints=tuple(sections["constraints"]),
            decisions=tuple(sections["decisions"]),
            open_items=tuple(sections["open_items"]),
            archived=tuple(archived),
        )

    def preview_models(self) -> dict[str, str]:
        """只解析模型名供 UI 展示；不读取密钥文件、不校验 Key、也不构建 Provider。"""
        current = self.current
        try:
            previews = dict(
                self._model_preview_resolver(
                    current.record.workspace if current is not None else self._entry_workspace,
                    environ=self.options.environ,
                    model=self.options.model,
                )
            )
        except (ConfigError, OSError, ValueError) as exc:
            raise SessionRuntimeError("无法解析模型预览") from exc
        if current is not None:
            previews[current.record.provider] = current.record.model
        elif self._entry_model:
            previews[self._entry_provider] = self._entry_model
        return previews

    def _ensure_session_for_task_locked(self) -> ActiveSession:
        """在任务互斥锁内为首次普通输入创建并发布唯一真实 Session。"""

        if self.current is not None:
            return self.current
        if self._pending_cleanup:
            raise SessionRuntimeError("旧任务资源清理尚未确认，禁止创建会话")
        candidate: ActiveSession | None = None
        ownership: SessionLock | None = None
        try:
            candidate, ownership = self._prepare_new_active(
                None,
                self._entry_workspace,
                self._entry_provider,
                self._entry_model,
                memory=SessionMemory(permission_level=self._entry_permission),
            )
            # 候选构建可能耗时；退出/取消在提交前到达时不得产生持久化会话。
            with self._task_state_lock:
                cancelled = bool(
                    self._task_cancellation is not None
                    and self._task_cancellation.is_cancelled
                )
                shutting_down = self._shutdown_requested
            if cancelled or shutting_down:
                raise SessionRuntimeError("首次任务已取消，会话未创建")
            record = self.store.insert_prepared(candidate.record)
        except BaseException as exc:
            if ownership is not None:
                ownership.close()
            if candidate is not None:
                self._prepared_spill_sessions.discard(candidate.record.id)
            if isinstance(exc, SessionRuntimeError):
                raise
            if isinstance(exc, (SessionError, ConfigError, OSError, ValueError)):
                raise SessionRuntimeError("无法为首次任务创建会话") from exc
            raise
        self._handoff_session(replace(candidate, record=record), ownership)
        return self._require_active_session()

    def _prepare_new_active(
        self,
        name: str | None,
        workspace: Path,
        provider: str,
        model: str | None,
        *,
        memory: SessionMemory | None = None,
    ) -> tuple[ActiveSession, SessionLock]:
        """构建不写 SQLite 的临时候选，供创建和首次启动共用。"""
        # 名称必须在任何配置、审计或 Agent 构建前校验，拒绝无效输入的副作用。
        validated_name = validate_session_name(name) if name is not None else "pending"
        if self._active_session_factory is None:
            config = self._load_config(workspace, provider, model)
            record = self.store.prepare_record(
                validated_name,
                config.workspace,
                config.provider.name,
                config.provider.model,
            )
        else:
            config = None
            record = self.store.prepare_record(
                validated_name, workspace, provider, model or "default",
            )
        if name is None:
            record = replace(record, name=self._automatic_session_name(record))
        ownership = self._acquire_session(record.id)
        try:
            return self._build_active(
                record,
                memory or SessionMemory(),
                config=config,
            ), ownership
        except BaseException:
            self._prepared_spill_sessions.discard(record.id)
            ownership.close()
            raise

    @staticmethod
    def _automatic_session_name(record: SessionRecord) -> str:
        """只用受控时间与真实 ID 生成标题，绝不接收用户任务正文。"""

        try:
            created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise SessionError("会话时间无效") from exc
        short_id = "".join(character for character in record.id if character.isalnum())[:8]
        if not short_id:
            raise SessionError("会话标识无效")
        return validate_session_name(
            f"会话-{created.strftime('%Y%m%d-%H%M%S')}-{short_id}"
        )

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
                    registry.context.clarifier = self._clarifier
                except (AttributeError, TypeError) as exc:
                    raise SessionRuntimeError("自定义会话工厂无法绑定现有变更账本") from exc
                if registry.context.change_journal is not active_journal:
                    raise SessionRuntimeError("自定义会话工厂无法绑定现有变更账本")
            active = replace(
                candidate,
                context=replace(candidate.context, unknown_effects=memory.unknown_effects or candidate.context.unknown_effects),
                tools=candidate.tools or (registries[0] if registries else None),
                journal=active_journal,
            )
            return self._restore_conversation_memory(active)
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
                clarifier=self._clarifier,
            )
        )
        if loaded.audit_dir is None:
            raise SessionRuntimeError("运行配置缺少审计目录")
        audit = self._audit_factory(loaded.audit_dir / f"session-{record.id}.jsonl")
        audit.prepare()
        agent = self._create_agent(loaded, tools, audit)
        active = ActiveSession(
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
        return self._restore_conversation_memory(active)

    def _restore_conversation_memory(self, active: ActiveSession) -> ActiveSession:
        """仅 reviewed_summary 模式加载任务意图，不恢复批准或验证能力。"""

        if active.config.memory.persistence != "reviewed_summary":
            return active
        loaded = self.store.load_conversation_memory(
            active.record.id,
            max_chars=active.config.memory.summary_max_chars,
        )
        if loaded is None:
            return active
        memory, next_message_seq = loaded
        context = replace(
            active.context,
            conversation_memory=memory,
            next_message_seq=next_message_seq,
            latest_completed_task_seq=memory.covered_through,
            persisted_memory_revision=memory.revision,
            # 加载语义记忆不能恢复或提升任何可信执行状态。
            verification_evidence=None,
        )
        return replace(active, context=context)

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
        if "memory_config" in agent_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in agent_parameters.values()
        ):
            agent_kwargs["memory_config"] = config.memory
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
        """仅保留当前拥有的 Session；释放所有权后不能复用旧对象。"""
        if self.current is None:
            self._session_cache = {}
        else:
            self._session_cache = {self.current.record.id: self.current}
