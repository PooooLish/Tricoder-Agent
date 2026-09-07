"""交互会话的运行时装配、原子切换与安全摘要持久化。"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Callable, Concatenate, Mapping, ParamSpec, Protocol, TypeVar

from tricoder.agent import AgentObserver, CodingAgent
from tricoder.audit import AuditLogger
from tricoder.changes import (
    ChangeJournal,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
    render_change_set_diff,
)
from tricoder.core.cancellation import CancellationToken
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
        self._task_session_id: str | None = None
        self._task_permission: str | None = None
        self._task_cancellation: CancellationToken | None = None

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
        self.current = candidate
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
    def clear_current(self) -> None:
        """清除消息和摘要，但保留文件路径与验证状态等结构化元数据。"""
        original = self.current
        memory = SessionMemory(
            modified_files=original.memory.modified_files,
            verification=original.memory.verification,
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
        if self._task_active:
            self._task_lock.release()
            raise SessionRuntimeError("已有 Agent 任务正在运行")
        try:
            # 令牌与活动标志作为一个快照发布，避免取消线程观察到
            # ``active=True`` 但令牌仍为空的短暂窗口。
            with self._task_state_lock:
                self._task_cancellation = CancellationToken()
                self._task_active = True
            self._task_session_id = self.current.record.id
            self._task_permission = self.current.memory.permission_level
            try:
                return self._run_task_locked(task)
            finally:
                if self.current.record.id != self._task_session_id:
                    raise SessionRuntimeError(
                        "任务运行期间会话被切换，拒绝更新该会话"
                    )
        finally:
            with self._task_state_lock:
                self._task_active = False
                self._task_cancellation = None
            self._task_session_id = None
            self._task_permission = None
            self._task_lock.release()

    def cancel_current(self) -> bool:
        """无须获取任务锁即可线程安全地请求取消当前任务。"""

        with self._task_state_lock:
            token = self._task_cancellation
            if not self._task_active or token is None:
                return False
            return token.cancel()

    def _run_task_locked(self, task: str) -> RunResult:
        original = self.current
        original.journal.begin_task(
            tuple(original.context.modified_files),
            original.context.verification,
        )
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
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                if self._task_cancellation is not None:
                    self._task_cancellation.cancel()
            try:
                original.journal.seal_task(
                    tuple(original.context.modified_files),
                    original.context.verification,
                )
            except Exception:
                # Agent 主异常必须原样越过 Runtime；账本收尾异常不能替换根因。
                pass
            raise
        original.journal.seal_task(
            tuple(turn.context.modified_files),
            turn.context.verification,
        )
        result = turn.result
        verification = _normalized_verification(result.verification)
        persisted_summary = _persisted_run_summary(result, verification)
        memory = SessionMemory(
            summary=persisted_summary,
            requirements_summary=safe_requirement_summary(task),
            last_task_summary=persisted_summary,
            modified_files=tuple(result.modified_files),
            verification=verification,
            permission_level=original.memory.permission_level,
        )
        self.current = replace(original, memory=memory, context=turn.context)
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        self._persist_current()
        return result

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
        memory = replace(
            self.current.memory,
            modified_files=change_set.before_modified_files,
            verification=change_set.before_verification,
        )
        context = replace(
            self.current.context,
            modified_files=change_set.before_modified_files,
            verification=change_set.before_verification,
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
