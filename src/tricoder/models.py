"""跨模块共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tricoder.execution_state import ErrorCode, FileEffects, RecoveryAction, ToolError

if TYPE_CHECKING:
    from tricoder.core.clarification import ClarificationResult
    from tricoder.core.validation import CommandCheckRecord, TaskValidationReport
    from tricoder.workspace.verification import VerificationEvidence, WorkspaceSnapshot


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Provider 无关的可调用工具说明。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("工具名称必须是非空字符串")
        if not isinstance(self.parameters, dict):
            raise ValueError("工具参数 Schema 必须是对象")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一次 Provider 已解析的工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("工具调用 ID 必须是非空字符串")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("工具调用名称必须是非空字符串")
        if not isinstance(self.arguments, dict):
            raise ValueError("工具调用参数必须是对象")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.cache_miss_tokens,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Token 用量必须是非负整数或 None")

    def merge(self, other: "TokenUsage") -> "TokenUsage":
        def add(left: int | None, right: int | None) -> int | None:
            values = [value for value in (left, right) if value is not None]
            return sum(values) if values else None

        return TokenUsage(
            add(self.input_tokens, other.input_tokens),
            add(self.output_tokens, other.output_tokens),
            add(self.cached_tokens, other.cached_tokens),
            add(self.cache_miss_tokens, other.cache_miss_tokens),
        )

    @property
    def cache_hit_ratio(self) -> float | None:
        if self.input_tokens is None or self.input_tokens <= 0:
            return None
        if self.cached_tokens is None:
            return None
        return self.cached_tokens / self.input_tokens


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Provider 适配器归一化后的响应，不保留厂商原始对象。"""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """发送给模型的一条消息。"""

    role: str
    content: str | None
    kind: str = "generic"
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    # 会话历史的稳定本地序号；Provider 序列化刻意忽略这些元数据。
    message_seq: int | None = None
    task_id: str | None = None

    def __post_init__(self) -> None:
        """确保不同角色只携带其允许的结构化字段。"""

        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("消息角色不受支持")
        if not isinstance(self.content, (str, type(None))):
            raise ValueError("消息内容必须是字符串或 None")
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(call, ToolCall) for call in self.tool_calls
        ):
            raise ValueError("消息工具调用必须是 ToolCall 元组")
        if self.tool_call_id is not None and (
            not isinstance(self.tool_call_id, str) or not self.tool_call_id
        ):
            raise ValueError("工具调用 ID 必须是非空字符串")
        if self.message_seq is not None and (
            type(self.message_seq) is not int or self.message_seq <= 0
        ):
            raise ValueError("消息序号必须是正整数或 None")
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id.strip()
        ):
            raise ValueError("任务 ID 必须是非空字符串或 None")

        if self.role == "tool":
            if self.content is None:
                raise ValueError("工具结果必须包含文本内容")
            if self.tool_calls:
                raise ValueError("工具结果不能携带新的工具调用")
            if self.tool_call_id is None:
                raise ValueError("工具结果必须包含 tool_call_id")
            return

        if self.tool_call_id is not None:
            raise ValueError("普通消息不能携带 tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("只有 assistant 消息可以携带工具调用")
        if self.content is None and not self.tool_calls:
            raise ValueError("普通消息必须包含文本内容")

    def as_dict(self) -> dict[str, str | None]:
        return {"role": self.role, "content": self.content}

    def character_budget(self) -> int:
        """返回消息在上下文预算中占用的统一字符数估算。"""

        return len(self.role) + len(self.content or "") + len(self.tool_call_id or "") + sum(
            len(call.id) + len(call.name) + len(str(call.arguments))
            for call in self.tool_calls
        )


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """单个模型服务的连接配置。"""

    name: str
    api_key: str = field(repr=False)
    base_url: str
    model: str


@dataclass(frozen=True, slots=True)
class ExtensionsConfig:
    """所有真实扩展的总开关，默认关闭。"""

    enabled: bool = False


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """单个 MCP 声明；只保存环境变量名和存在性，不保存凭据值。"""

    id: str
    transport: str
    command: str
    args: tuple[str, ...] = ()
    enabled: bool = False
    trust: str = "project"
    credential_env: tuple[str, ...] = ()
    credentials_authorized: bool = True
    credentials_present: bool = True


@dataclass(frozen=True, slots=True)
class MCPConfig:
    """MCP 家族总开关与严格解析后的服务器声明。"""

    enabled: bool = False
    servers: tuple[MCPServerConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillsConfig:
    """项目 Skill 发现配置；加载器将在 Phase 6 实现。"""

    enabled: bool = False
    project_dir: str = ".tricoder/skills"


@dataclass(frozen=True, slots=True)
class HooksConfig:
    """Hook 总开关；执行引擎将在 Phase 7 实现。"""

    enabled: bool = False


@dataclass(frozen=True, slots=True)
class WorktreeConfig:
    """Worktree 能力开关，默认关闭。"""

    enabled: bool = False


@dataclass(frozen=True, slots=True)
class AgentsConfig:
    """子 Agent 安全默认值；协调器将在后续阶段实现。"""

    enabled: bool = False
    max_depth: int = 1
    max_concurrency: int = 1
    default_read_only: bool = True


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """默认启用结构化压缩与经用户审阅后保存的会话记忆。"""

    compaction: str = "structured"
    persistence: str = "reviewed_summary"
    trigger_ratio: float = 0.80
    target_ratio: float = 0.65
    summary_max_chars: int = 6_000
    summary_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.compaction not in {"off", "structured"}:
            raise ValueError("memory.compaction 只能是 off 或 structured")
        if self.persistence not in {"off", "reviewed_summary"}:
            raise ValueError("memory.persistence 只能是 off 或 reviewed_summary")
        if self.persistence == "reviewed_summary" and self.compaction != "structured":
            raise ValueError("reviewed_summary 要求 compaction=structured")
        if not (
            isinstance(self.target_ratio, (int, float))
            and not isinstance(self.target_ratio, bool)
            and isinstance(self.trigger_ratio, (int, float))
            and not isinstance(self.trigger_ratio, bool)
            and 0 < self.target_ratio < self.trigger_ratio < 1
        ):
            raise ValueError("memory 比例必须满足 0 < target_ratio < trigger_ratio < 1")
        if (
            type(self.summary_max_chars) is not int
            or not 1 <= self.summary_max_chars <= 20_000
        ):
            raise ValueError("memory.summary_max_chars 必须在 1 到 20000 之间")
        if (
            not isinstance(self.summary_timeout_seconds, (int, float))
            or isinstance(self.summary_timeout_seconds, bool)
            or not 0 < self.summary_timeout_seconds <= 120
        ):
            raise ValueError("memory.summary_timeout_seconds 必须在 0 到 120 秒之间")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次 Agent 运行所需的完整配置。"""

    workspace: Path
    provider: ProviderConfig
    max_rounds: int = 30
    timeout: float = 30.0
    read_only: bool = False
    env_file: Path | None = None
    key_source: str = "进程环境变量"
    audit_dir: Path | None = None
    max_context_chars: int = 80_000
    tool_protocol: str = "native"
    plan_enabled: bool = True
    extensions: ExtensionsConfig = field(default_factory=ExtensionsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


@dataclass(frozen=True, slots=True)
class ToolAction:
    """模型请求执行的单个结构化动作。"""

    tool: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行后返回给 Agent 的统一结果。"""

    ok: bool
    output: str
    relative_path: str | None = None
    modified_paths: tuple[str, ...] = ()
    audit_paths: tuple[str, ...] = ()
    change_chars: int = 0
    # None 表示该工具不产生验证结论；True/False 仅由认可的测试/编译/
    # 静态检查命令设置，git 只读与普通脚本不改变验证状态。
    verification_passed: bool | None = None
    # 大型输出落盘后只公开不可猜引用和审计元数据；绝不包含本机路径。
    spill_reference: str | None = None
    spill_bytes: int = 0
    spill_sha256: str | None = None
    file_effects: FileEffects | None = None
    error: ToolError | None = None
    verification_evidence: VerificationEvidence | None = None
    # 由本地命令执行器签发的任务级检查事实；它不等同于文件状态验证证据。
    command_check: CommandCheckRecord | None = None
    # 仅本地 ask_user 处理器可签发；回答只是信息，不携带审批能力。
    clarification: ClarificationResult | None = None
    # 工具注册表在截断或 spill 前对完整正文生成；仅用于进展检测，不持久化正文。
    # 扩展自报值会被宿主覆盖，Runner 不应从展示引用反推内容身份。
    progress_output_digest: str | None = None


def tool_failure(
    code: ErrorCode, output: str, *, recovery: RecoveryAction | None = None,
    **metadata: Any,
) -> ToolResult:
    """由本地错误产生点选择类别；可重新规划不等于允许自动重试。"""
    if recovery is None:
        recovery = (RecoveryAction.REPLAN if code in {
            ErrorCode.UNKNOWN_TOOL, ErrorCode.INVALID_ARGUMENT, ErrorCode.EXECUTION_FAILED,
        } else RecoveryAction.STOP_TASK)
    return ToolResult(False, output, error=ToolError(code, recovery), **metadata)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """持久化会话的稳定元数据。"""

    id: str
    name: str
    workspace: Path
    provider: str
    model: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SessionMemory:
    """可安全恢复的会话摘要与结构化运行状态。"""

    summary: str = ""
    requirements_summary: str = ""
    last_task_summary: str = ""
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"
    permission_level: str = "strict"
    unknown_effects: bool = False
    # 宿主签发的跨任务验证义务；与 verification 展示字符串相互独立。
    verification_obligation: Literal["none", "pending", "legacy_unknown"] = "none"
    pending_verification_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunResult:
    """Agent 整体运行结果。"""

    ok: bool
    summary: str
    rounds: int
    tool_calls: int = 0
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"
    usage: TokenUsage | None = None
    unknown_effects: bool = False
    cleanup_failed: bool = False
    modified_directories: tuple[str, ...] = ()
    # 追加默认值以保持既有位置参数和调用方兼容。
    task_validation: TaskValidationReport = field(
        default_factory=lambda: _empty_task_validation_report()
    )
    # 以下三个字段只描述宿主状态，不接受 Provider 或语义记忆自报。
    current_verification_required: bool = False
    verification_obligation: Literal["none", "pending", "legacy_unknown"] = "none"
    pending_verification_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cleanup_failed:
            object.__setattr__(self, "ok", False)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """一个会话在当前进程内可复用的不可变快照。"""

    messages: tuple[Message, ...] = ()
    persisted_summary: str = ""
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"
    unknown_effects: bool = False
    verification_evidence: VerificationEvidence | None = None
    verification_failure: WorkspaceSnapshot | None = None
    verification_required: bool = False
    # 延迟导入会形成循环；default_factory 在实例化时再解析纯记忆类型。
    conversation_memory: Any = field(
        default_factory=lambda: _empty_conversation_memory()
    )
    # 独立的待保存候选可以覆盖仍保留在内存中的近期完整任务；它不会自动
    # 注入下一轮模型请求，也不会改变运行时压缩边界。
    review_memory_candidate: Any | None = None
    next_message_seq: int = 1
    # 仅由 Agent 在任务终态且连续历史闭合后推进；本地记忆编辑不会改变该水位。
    # 字段名为数据库兼容保留，语义是“最新可纳入记忆的已结束任务”。
    latest_completed_task_seq: int = 0
    persisted_memory_revision: int | None = None
    memory_pending_clear: bool = False
    # 仅用于下一次 Provider 请求的本地可信提醒；Agent 返回时必须清空，
    # 不进入持久化摘要、语义记忆或历史消息。
    workspace_change_notice: str = ""
    # 仅进程内跟踪目录副作用；本轮不修改 SQLite schema。
    modified_directories: tuple[str, ...] = ()
    # 跨任务义务与上面的单任务门禁分开；恢复时只恢复这里，不恢复 evidence。
    verification_obligation: Literal["none", "pending", "legacy_unknown"] = "none"
    pending_verification_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.next_message_seq) is not int or self.next_message_seq <= 0:
            raise ValueError("下一消息序号必须是正整数")
        if (
            type(self.latest_completed_task_seq) is not int
            or self.latest_completed_task_seq < 0
            or self.latest_completed_task_seq >= self.next_message_seq
        ):
            raise ValueError("最新完成任务序号必须位于已分配消息范围内")
        if self.persisted_memory_revision is not None and (
            type(self.persisted_memory_revision) is not int
            or self.persisted_memory_revision < 0
        ):
            raise ValueError("已持久化记忆 revision 必须是非负整数或 None")
        if not isinstance(self.workspace_change_notice, str):
            raise ValueError("工作区变化提醒必须是字符串")


def _empty_conversation_memory() -> Any:
    """在不让纯记忆模块与共享模型循环导入的前提下提供默认值。"""

    from tricoder.context.memory import ConversationMemory

    return ConversationMemory()


def _empty_task_validation_report() -> Any:
    """延迟创建验证报告，避免 core.events 与共享 models 循环导入。"""

    from tricoder.core.validation import TaskValidationReport

    return TaskValidationReport()


@dataclass(frozen=True, slots=True)
class SessionTurnResult:
    """一次会话轮次的运行结果及更新后的上下文。"""

    result: RunResult
    context: SessionContext
    # 仅当 Agent 已消费本轮所有已执行工具的副作用时为真；不持久化。
    file_effects_observed: bool = False
    # 摘要请求不计入业务 usage/round/tool_calls；None 表示 Provider 未报告。
    memory_usage: TokenUsage | None = None
    memory_calls: int = 0
    memory_warning: str = ""


@dataclass(frozen=True, slots=True)
class MemoryRefreshResult:
    """显式整理待保存候选的结果；不携带业务任务或工具执行结果。"""

    context: SessionContext
    memory_usage: TokenUsage | None = None
    memory_calls: int = 0
