"""跨模块共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Message:
    """发送给模型的一条消息。"""

    role: str
    content: str
    kind: str = "generic"

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """单个模型服务的连接配置。"""

    name: str
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次 Agent 运行所需的完整配置。"""

    workspace: Path
    provider: ProviderConfig
    max_rounds: int = 12
    timeout: float = 30.0
    read_only: bool = False
    env_file: Path | None = None
    key_source: str = "进程环境变量"
    audit_dir: Path | None = None
    max_context_chars: int = 80_000


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


@dataclass(frozen=True, slots=True)
class RunResult:
    """Agent 整体运行结果。"""

    ok: bool
    summary: str
    rounds: int
    tool_calls: int = 0
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"


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
