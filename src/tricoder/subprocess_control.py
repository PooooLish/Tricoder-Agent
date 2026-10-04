"""兼容入口；进程执行实现已迁至 :mod:`tricoder.process.control`。"""

from tricoder.process.control import (
    BoundedProcessResult,
    ProcessExecutionUncertain,
    _WindowsJob,
    run_bounded_process,
)

__all__ = [
    "BoundedProcessResult",
    "ProcessExecutionUncertain",
    "_WindowsJob",
    "run_bounded_process",
]
