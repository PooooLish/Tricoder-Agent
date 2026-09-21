"""Docker 沙箱的执行、副本和受控发布边界。"""

from tricoder.sandbox.execution import (
    ExecutionBackend,
    ExecutionBackendError,
    ExecutionRequest,
    ExecutionResult,
    ExecutionUncertain,
    LocalExecutionBackend,
    build_command_policy,
    build_execution_backend,
)

__all__ = [
    "ExecutionBackend",
    "ExecutionBackendError",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionUncertain",
    "LocalExecutionBackend",
    "build_command_policy",
    "build_execution_backend",
]
