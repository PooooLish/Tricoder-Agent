"""跨模块共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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


@dataclass(frozen=True, slots=True)
class SessionContext:
    """一个会话在当前进程内可复用的不可变快照。"""

    messages: tuple[Message, ...] = ()
    persisted_summary: str = ""
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"


@dataclass(frozen=True, slots=True)
class SessionTurnResult:
    """一次会话轮次的运行结果及更新后的上下文。"""

    result: RunResult
    context: SessionContext
