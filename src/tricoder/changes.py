"""任务内文件变更的纯内存日志。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


MAX_TASK_CHANGE_CHARS = 2_000_000


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """文件在某个时点的设备与 inode 标识。"""

    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """文件在某个时点的可恢复快照。"""

    path: str
    content: str
    mode: int
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class FileChange:
    """一个路径从任务开始到任务结束的净变化。"""

    path: str
    before: FileSnapshot | None
    after: FileSnapshot | None


@dataclass(frozen=True, slots=True)
class TaskChangeSet:
    """已封存任务的变更与验证元数据。"""

    changes: tuple[FileChange, ...]
    before_modified_files: tuple[str, ...]
    before_verification: str
    after_modified_files: tuple[str, ...]
    after_verification: str


@dataclass(frozen=True, slots=True)
class UndoPreview:
    """撤销前展示的反向差异与规范相对路径。"""

    diff: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UndoExecution:
    """一次全量撤销的结构化结果，不包含任何源码快照。"""

    ok: bool
    paths: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    compensation_failed: tuple[str, ...] = ()


def render_change_set_diff(change_set: TaskChangeSet, *, reverse: bool = False) -> str:
    """按规范路径顺序渲染任务正向或反向 unified diff。"""

    chunks: list[str] = []
    for change in sorted(change_set.changes, key=lambda item: item.path):
        source = change.after if reverse else change.before
        target = change.before if reverse else change.after
        chunks.append(
            "".join(
                difflib.unified_diff(
                    source.content.splitlines(keepends=True) if source else [],
                    target.content.splitlines(keepends=True) if target else [],
                    fromfile=change.path if source else "/dev/null",
                    tofile=change.path if target else "/dev/null",
                )
            )
        )
    return "".join(chunks)


class ChangeBudgetError(ValueError):
    """表示任务变更超过内存字符预算。"""


class ChangeJournalError(RuntimeError):
    """表示变更日志的调用顺序或数据无效。"""


class ChangeJournal:
    """聚合单个任务内的文件净变化，并保留最近一次非空结果。"""

    def __init__(self, max_chars: int = MAX_TASK_CHANGE_CHARS) -> None:
        self._max_chars = max_chars
        self._active_changes: dict[str, FileChange] | None = None
        self._before_modified_files: tuple[str, ...] = ()
        self._before_verification = ""
        self._latest: TaskChangeSet | None = None

    def begin_task(self, modified_files: tuple[str, ...], verification: str) -> None:
        """开始记录一个任务；同一时间只允许一个活动任务。"""

        if self._active_changes is not None:
            raise ChangeJournalError("当前任务尚未封存")
        self._active_changes = {}
        self._before_modified_files = modified_files
        self._before_verification = verification

    def reserve(self, proposed: tuple[FileChange, ...]) -> None:
        """确认拟议变更加入后仍处于任务字符预算内。"""

        changes = self._require_active_changes()
        projected = self._project(changes, proposed)
        chars = sum(
            len(snapshot.content)
            for change in projected.values()
            for snapshot in (change.before, change.after)
            if snapshot is not None
        )
        if chars > self._max_chars:
            raise ChangeBudgetError("变更预算不足：预计字符数超过限制")

    def record_committed(
        self,
        path: str,
        before: FileSnapshot | None,
        after: FileSnapshot | None,
    ) -> None:
        """记录一次已提交写入，保留该路径的最早前态与最新后态。"""

        changes = self._require_active_changes()
        self._validate_change(FileChange(path, before, after))
        self._apply(changes, FileChange(path, before, after))

    def seal_task(
        self, modified_files: tuple[str, ...], verification: str
    ) -> TaskChangeSet | None:
        """封存活动任务；没有净变化时保留此前的最近结果。"""

        changes = self._require_active_changes()
        result = (
            TaskChangeSet(
                changes=tuple(changes.values()),
                before_modified_files=self._before_modified_files,
                before_verification=self._before_verification,
                after_modified_files=modified_files,
                after_verification=verification,
            )
            if changes
            else None
        )
        self._active_changes = None
        self._before_modified_files = ()
        self._before_verification = ""
        if result is not None:
            self._latest = result
        return result

    def latest(self) -> TaskChangeSet | None:
        """返回最近一次有净变化的已封存任务。"""

        return self._latest

    def clear_latest(self) -> None:
        """清除最近一次可撤销任务。"""

        self._latest = None

    def _require_active_changes(self) -> dict[str, FileChange]:
        if self._active_changes is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return self._active_changes

    def _project(
        self, changes: dict[str, FileChange], proposed: tuple[FileChange, ...]
    ) -> dict[str, FileChange]:
        projected = dict(changes)
        for change in proposed:
            self._validate_change(change)
            self._apply(projected, change)
        return projected

    def _apply(self, changes: dict[str, FileChange], change: FileChange) -> None:
        existing = changes.get(change.path)
        merged = FileChange(
            path=change.path,
            before=existing.before if existing is not None else change.before,
            after=change.after,
        )
        if self._is_net_zero(merged):
            changes.pop(change.path, None)
        else:
            changes[change.path] = merged

    @staticmethod
    def _validate_change(change: FileChange) -> None:
        for snapshot in (change.before, change.after):
            if snapshot is not None and snapshot.path != change.path:
                raise ChangeJournalError("变更路径与快照路径不一致")

    @staticmethod
    def _is_net_zero(change: FileChange) -> bool:
        if change.before is None and change.after is None:
            return True
        if change.before is None or change.after is None:
            return False
        return (
            change.before.content == change.after.content
            and change.before.mode == change.after.mode
        )
