"""兼容入口；工作区锁实现已迁至 :mod:`tricoder.workspace.lock`。"""

from tricoder.workspace.lock import (
    ACTIVITY_FILENAME,
    CONTROL_DIRECTORY,
    GUARD_DIRECTORY_NAME,
    LOCK_FILENAME,
    WorkspaceIdentityError,
    WorkspaceLock,
    WorkspaceLockBusyError,
    WorkspaceLockError,
    WorkspaceRecoveryRequiredError,
    _is_reparse,
)

__all__ = [
    "ACTIVITY_FILENAME",
    "CONTROL_DIRECTORY",
    "GUARD_DIRECTORY_NAME",
    "LOCK_FILENAME",
    "WorkspaceIdentityError",
    "WorkspaceLock",
    "WorkspaceLockBusyError",
    "WorkspaceLockError",
    "WorkspaceRecoveryRequiredError",
    "_is_reparse",
]
