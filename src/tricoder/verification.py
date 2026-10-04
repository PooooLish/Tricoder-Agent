"""兼容入口；验证证据实现已迁至 :mod:`tricoder.workspace.verification`。"""

from tricoder.workspace.verification import (
    VerificationEvidence,
    VerificationScope,
    WorkspaceSnapshot,
    _bound_directory,
    _is_reparse,
    _metadata,
    _open_binary,
    capture_workspace,
    proves_new_file_version,
    stable_snapshots,
)

__all__ = [
    "VerificationEvidence",
    "VerificationScope",
    "WorkspaceSnapshot",
    "_bound_directory",
    "_is_reparse",
    "_metadata",
    "_open_binary",
    "capture_workspace",
    "proves_new_file_version",
    "stable_snapshots",
]
