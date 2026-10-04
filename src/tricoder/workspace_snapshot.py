"""兼容入口；工作区快照实现已迁至 :mod:`tricoder.workspace.snapshot`。"""

from tricoder.workspace.snapshot import (
    FileSnapshotEntry,
    SnapshotLimits,
    WorkspaceBaseline,
    WorkspaceChange,
    WorkspaceChangePreview,
    WorkspaceScanError,
    capture_workspace_baseline,
    compare_baselines,
    task_changes_match_baselines,
)

__all__ = [
    "FileSnapshotEntry",
    "SnapshotLimits",
    "WorkspaceBaseline",
    "WorkspaceChange",
    "WorkspaceChangePreview",
    "WorkspaceScanError",
    "capture_workspace_baseline",
    "compare_baselines",
    "task_changes_match_baselines",
]
