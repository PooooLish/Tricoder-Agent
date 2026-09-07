"""TriCoder 的 MCP 适配边界。

此包导入时不得加载第三方 MCP SDK，以保证未启用 MCP 的路径保持零依赖。
"""

from .client import (
    MCPClient,
    MCPClientError,
    MCPCleanupError,
    MCPProtocolError,
    MCPTimeoutError,
)
from .sdk import MCPDependencyError
from .transport import (
    MCPProcessExitEvidence,
    MCPStdioBindings,
    MCPTransportOutcome,
    VerifiedStdioTransport,
)

__all__ = [
    "MCPClient",
    "MCPClientError",
    "MCPCleanupError",
    "MCPDependencyError",
    "MCPProtocolError",
    "MCPProcessExitEvidence",
    "MCPStdioBindings",
    "MCPTimeoutError",
    "MCPTransportOutcome",
    "VerifiedStdioTransport",
]
