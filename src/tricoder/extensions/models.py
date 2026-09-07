"""扩展宿主的不可变描述、信任、失败与工具来源模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from tricoder.core.cancellation import CancellationToken

if TYPE_CHECKING:
    from tricoder.tools.handlers import ToolHandler


_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SOURCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_TOOL_KINDS = frozenset({"builtin", "mcp", "hook", "worktree", "agent"})
_TOOL_RISKS = frozenset({"read", "write", "process", "network", "dangerous"})


class ExtensionKind(str, Enum):
    """当前 Extension Host 认识的扩展类别。"""

    MCP = "mcp"
    SKILL = "skill"
    HOOK = "hook"


class ExtensionTrust(str, Enum):
    """扩展声明来自哪个信任边界，而不是“代码一定安全”的承诺。"""

    BUILTIN = "builtin"
    USER = "user"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class ExtensionDescriptor:
    """可向 doctor/审计公开的扩展描述，不保存路径、URL 或凭据值。"""

    id: str
    kind: ExtensionKind
    source: str
    enabled: bool
    trust: ExtensionTrust

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.id):
            raise ValueError("扩展 id 必须是规范化安全标识")
        if not isinstance(self.kind, ExtensionKind):
            raise ValueError("扩展 kind 无效")
        if not isinstance(self.source, str) or not _SOURCE_PATTERN.fullmatch(self.source):
            raise ValueError("扩展 source 必须是不含认证信息的安全标识")
        if not isinstance(self.enabled, bool):
            raise ValueError("扩展 enabled 必须是布尔值")
        if not isinstance(self.trust, ExtensionTrust):
            raise ValueError("扩展 trust 无效")


@dataclass(frozen=True, slots=True)
class ExtensionFailure:
    """扩展失败的固定安全分类；不携带底层异常正文。"""

    extension_id: str
    phase: str
    safe_message: str
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolOrigin:
    """动态或内置工具的来源及强制风险声明。"""

    kind: str
    id: str
    risk: str

    def __post_init__(self) -> None:
        if self.kind not in _TOOL_KINDS:
            raise ValueError("工具来源 kind 无效")
        if not isinstance(self.id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.id):
            raise ValueError("工具来源 id 必须是规范化安全标识")
        if self.risk not in _TOOL_RISKS:
            raise ValueError("工具必须声明有效风险等级")


class ExtensionProvider(Protocol):
    """由 MCP、Skills 或 Hooks 适配器实现的最小生命周期协议。"""

    descriptor: ExtensionDescriptor

    async def start(self, cancellation: CancellationToken) -> None: ...

    async def stop(self) -> None: ...

    def tool_handlers(self) -> tuple["ToolHandler", ...]: ...

    def prompt_fragments(self) -> tuple[str, ...]: ...
