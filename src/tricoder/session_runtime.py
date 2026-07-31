"""交互会话的运行时装配、原子切换与安全摘要持久化。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol

from tricoder.agent import AgentObserver, CodingAgent
from tricoder.audit import AuditLogger
from tricoder.config import AppConfig, ConfigError, load_config, preview_provider_models
from tricoder.models import (
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionMemory,
    SessionRecord,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ModelProvider, create_provider
from tricoder.sessions import (
    SessionError,
    SessionStore,
    safe_requirement_summary,
    validate_session_name,
)
from tricoder.tools import ToolContext, ToolRegistry


class SessionRuntimeError(RuntimeError):
    """会话装配、切换或内存持久化无法安全完成。"""


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


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """当前进程内一个会话的独立运行快照。"""

    record: SessionRecord
    memory: SessionMemory
    context: SessionContext
    config: AppConfig
    agent: "ContextAgent"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """供交互层显示的会话与持久化状态，不包含敏感内容。"""

    record: SessionRecord
    unsaved_memory: bool
    warning: str = ""


class ContextAgent(Protocol):
    """运行时只依赖可复用上下文的 Agent 接口，方便注入假实现。"""

    def run_with_context(self, task: str, context: SessionContext):  # type: ignore[no-untyped-def]
        """运行任务并返回结果与更新后的上下文。"""


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
        command_policy_factory: Callable[[], CommandPolicy] = CommandPolicy,
        tool_registry_factory: Callable[[ToolContext], ToolRegistry] = ToolRegistry,
        agent_factory: Callable[..., ContextAgent] = CodingAgent,
        audit_factory: Callable[[Path], AuditLogger] = AuditLogger,
        approver: Callable[[str, str], bool] | None = None,
        observer: AgentObserver | None = None,
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
        self._unsaved_memory = False
        self._warning = ""
        self._memory_dirty = False
        self._session_cache: dict[str, ActiveSession] = {}

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
        if self._memory_dirty and not self.persist_current():
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
        if self._memory_dirty and not self.persist_current():
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

    def rename_current(self, name: str) -> SessionRecord:
        """先持久化重命名，成功后才替换内存中的会话元数据。"""
        try:
            record = self.store.rename(self.current.record.id, name)
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("会话重命名失败") from exc
        self.current = replace(self.current, record=record)
        self._cache_current()
        return record

    def clear_current(self) -> None:
        """清除消息和摘要，但保留文件路径与验证状态等结构化元数据。"""
        original = self.current
        memory = SessionMemory(
            modified_files=original.memory.modified_files,
            verification=original.memory.verification,
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
        try:
            if not self.persist_current():
                raise OSError("会话记忆清除未持久化")
        except (SessionError, OSError) as exc:
            raise SessionRuntimeError("会话记忆清除未持久化") from exc

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
            rebuilt = self._build_active(candidate_record, original.memory, config=config)
            candidate = replace(
                rebuilt,
                memory=original.memory,
                context=original.context,
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
        """运行后仅提炼安全摘要和结构化元数据，绝不持久化原始消息。"""
        original = self.current
        turn = original.agent.run_with_context(task, original.context)
        result = turn.result
        verification = _normalized_verification(result.verification)
        persisted_summary = _persisted_run_summary(result, verification)
        memory = SessionMemory(
            summary=persisted_summary,
            requirements_summary=safe_requirement_summary(task),
            last_task_summary=persisted_summary,
            modified_files=tuple(result.modified_files),
            verification=verification,
        )
        self.current = replace(original, memory=memory, context=turn.context)
        self._cache_current()
        self._memory_dirty = memory != self._persisted_memory
        self.persist_current()
        return result

    def persist_current(self) -> bool:
        """尝试保存安全摘要；失败时保留内存状态并暴露未保存警告。"""
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

    def retry_persist(self) -> bool:
        """供退出流程再尝试一次保存，失败由调用方返回非零退出码。"""
        return self.persist_current()

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
    ) -> ActiveSession:
        """构建完整候选对象，调用方在成功返回前不会修改 ``current``。"""
        if self._active_session_factory is not None:
            return self._active_session_factory(record, memory, self.options)
        loaded = config or self._load_config(record.workspace, record.provider, record.model)
        provider = self._provider_factory(loaded.provider, loaded.timeout)
        workspace_policy = self._workspace_policy_factory(loaded.workspace)
        tools = self._tool_registry_factory(
            ToolContext(
                workspace_policy=workspace_policy,
                command_policy=self._command_policy_factory(),
                approver=self._approver,
                read_only=loaded.read_only,
                timeout=loaded.timeout,
            )
        )
        if loaded.audit_dir is None:
            raise SessionRuntimeError("运行配置缺少审计目录")
        audit = self._audit_factory(loaded.audit_dir / f"session-{record.id}.jsonl")
        audit.prepare()
        agent_kwargs = {
            "max_rounds": loaded.max_rounds,
            "max_context_chars": loaded.max_context_chars,
            "audit": audit,
            "observer": self._observer,
        }
        agent_parameters = inspect.signature(self._agent_factory).parameters
        if "tool_protocol" in agent_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in agent_parameters.values()
        ):
            agent_kwargs["tool_protocol"] = loaded.tool_protocol
        agent = self._agent_factory(provider, tools, **agent_kwargs)
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
        )

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
