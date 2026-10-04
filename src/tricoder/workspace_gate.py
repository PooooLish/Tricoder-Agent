"""兼容入口；工作区门禁实现已迁至 :mod:`tricoder.workspace.gate`。"""

from tricoder.workspace.gate import (
    ChangeObserver,
    Confirmer,
    Snapshotter,
    WorkspaceConfirmationRejected,
    WorkspaceConfirmationUnavailable,
    WorkspaceGate,
    WorkspaceGateCancelled,
    WorkspaceGateDecision,
    WorkspaceGateError,
    WorkspaceGatePreview,
)

__all__ = [
    "ChangeObserver",
    "Confirmer",
    "Snapshotter",
    "WorkspaceConfirmationRejected",
    "WorkspaceConfirmationUnavailable",
    "WorkspaceGate",
    "WorkspaceGateCancelled",
    "WorkspaceGateDecision",
    "WorkspaceGateError",
    "WorkspaceGatePreview",
]
