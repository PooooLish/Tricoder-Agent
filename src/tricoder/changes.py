"""任务内文件变更的纯内存日志。"""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass, field
import re

from tricoder.execution_state import EffectState, FileEffects


MAX_TASK_CHANGE_CHARS = 2_000_000
MAX_TASK_CREATED_DIRECTORIES = 128
_DIFF_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_NO_NEWLINE_MARKER = "\\ No newline at end of file\n"


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
class DirectorySnapshot:
    """目录在某个时点的权限与对象身份；不保存目录内容。"""

    path: str
    mode: int
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class DirectoryChange:
    """目录路径在任务中的净变化；首轮正常前向操作只会创建。"""

    path: str
    before: DirectorySnapshot | None
    after: DirectorySnapshot | None


@dataclass(frozen=True, slots=True)
class TaskChangeSet:
    """已封存任务的变更与验证元数据。"""

    changes: tuple[FileChange, ...]
    before_modified_files: tuple[str, ...]
    before_verification: str
    after_modified_files: tuple[str, ...]
    after_verification: str
    tainted_paths: tuple[str, ...] = ()
    directory_changes: tuple[DirectoryChange, ...] = ()
    before_modified_directories: tuple[str, ...] = ()
    after_modified_directories: tuple[str, ...] = ()
    tainted_directory_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UndoPreview:
    """撤销前展示的反向差异与规范相对路径。"""

    diff: str
    paths: tuple[str, ...]
    directory_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UndoExecution:
    """一次全量撤销的结构化结果；恢复证据仅供进程内收尾核验。"""

    ok: bool
    paths: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    compensation_failed: tuple[str, ...] = ()
    _restored_changes: tuple[FileChange, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    _restored_directory_changes: tuple[DirectoryChange, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    _compensated_change_set: TaskChangeSet | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def render_change_set_diff(change_set: TaskChangeSet, *, reverse: bool = False) -> str:
    """按规范路径顺序渲染任务正向或反向 unified diff。"""

    if change_set.tainted_paths or change_set.tainted_directory_paths:
        raise ChangeJournalError(
            "任务文件或目录状态冲突："
            f"{'、'.join((*change_set.tainted_paths, *change_set.tainted_directory_paths))}"
        )
    chunks: list[str] = []
    for change in sorted(change_set.changes, key=lambda item: item.path):
        source = change.after if reverse else change.before
        target = change.before if reverse else change.after
        chunks.append(
            render_file_diff(
                change.path,
                source.content if source else None,
                target.content if target else None,
                before_mode=source.mode if source else None,
                after_mode=target.mode if target else None,
            )
        )
    directory_changes = sorted(
        change_set.directory_changes,
        key=lambda item: (len(item.path.split("/")), item.path),
        reverse=reverse,
    )
    for change in directory_changes:
        source = change.after if reverse else change.before
        target = change.before if reverse else change.after
        if source is None and target is not None:
            chunks.append(f"+ directory {change.path}/\n")
        elif source is not None and target is None:
            chunks.append(f"- directory {change.path}/\n")
    return "".join(chunks)


def render_file_diff(
    path: str,
    before: str | None,
    after: str | None,
    *,
    before_mode: int | None = None,
    after_mode: int | None = None,
) -> str:
    """安全渲染单文件差异，并明确保留换行与权限语义。"""

    if before == after and before_mode == after_mode:
        return ""
    before_lines, before_missing_newline = _diff_input(before)
    after_lines, after_missing_newline = _diff_input(after)
    fromfile = path if before is not None else "/dev/null"
    tofile = path if after is not None else "/dev/null"
    rendered = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=fromfile,
            tofile=tofile,
        )
    )
    rendered = _mark_missing_final_newlines(
        rendered,
        len(before_lines),
        len(after_lines),
        before_missing_newline,
        after_missing_newline,
    )
    mode_lines: list[str] = []
    if (
        before_mode is not None
        and after_mode is not None
        and before_mode != after_mode
    ):
        mode_lines = [
            f"old mode {before_mode:04o}\n",
            f"new mode {after_mode:04o}\n",
        ]
    if rendered:
        rendered[2:2] = mode_lines
        return "".join(rendered)
    if mode_lines:
        return "".join([f"--- {fromfile}\n", f"+++ {tofile}\n", *mode_lines])
    if before is None and after == "":
        return (
            f"--- /dev/null\n+++ {path}\n"
            "@@ -0,0 +0,0 @@\n（创建空文件，内容为 0 字符）\n"
        )
    if before == "" and after is None:
        return (
            f"--- {path}\n+++ /dev/null\n"
            "@@ -0,0 +0,0 @@\n（删除空文件，内容为 0 字符）\n"
        )
    return ""


def _diff_input(content: str | None) -> tuple[list[str], bool]:
    if not content:
        return [], False
    missing_newline = not content.endswith("\n")
    return content.splitlines(keepends=True), missing_newline


def _mark_missing_final_newlines(
    rendered: list[str],
    before_line_count: int,
    after_line_count: int,
    before_missing_newline: bool,
    after_missing_newline: bool,
) -> list[str]:
    output: list[str] = []
    old_cursor = 0
    new_cursor = 0
    in_hunk = False
    for line in rendered:
        output.append(line if line.endswith("\n") else f"{line}\n")
        match = _DIFF_HUNK_HEADER.match(line)
        if match is not None:
            old_cursor = int(match.group(1))
            new_cursor = int(match.group(3))
            in_hunk = True
            continue
        if not in_hunk or not line:
            continue
        prefix = line[0]
        old_terminal = (
            prefix in " -"
            and before_missing_newline
            and old_cursor == before_line_count
        )
        new_terminal = (
            prefix in " +"
            and after_missing_newline
            and new_cursor == after_line_count
        )
        if prefix in " -":
            old_cursor += 1
        if prefix in " +":
            new_cursor += 1
        if old_terminal or new_terminal:
            output.append(_NO_NEWLINE_MARKER)
    return output


class ChangeBudgetError(ValueError):
    """表示任务变更超过内存字符预算。"""


class ChangeJournalError(RuntimeError):
    """表示变更日志的调用顺序或数据无效。"""


class ChangeJournal:
    """聚合单个任务内的文件净变化，并保留最近一次非空结果。"""

    def __init__(
        self,
        max_chars: int = MAX_TASK_CHANGE_CHARS,
        max_directories: int = MAX_TASK_CREATED_DIRECTORIES,
    ) -> None:
        self._max_chars = max_chars
        self._max_directories = max_directories
        self._active_changes: dict[str, FileChange] | None = None
        self._active_after: dict[str, FileSnapshot | None] | None = None
        self._active_tainted_paths: set[str] | None = None
        self._active_directory_changes: dict[str, DirectoryChange] | None = None
        self._active_directory_after: dict[
            str, DirectorySnapshot | None
        ] | None = None
        self._active_tainted_directory_paths: set[str] | None = None
        self._active_directory_reservations: set[str] | None = None
        self._before_modified_files: tuple[str, ...] = ()
        self._before_modified_directories: tuple[str, ...] = ()
        self._before_verification = ""
        self._latest: TaskChangeSet | None = None
        self._revision = 0
        self._revision_lock = threading.RLock()

    def begin_task(
        self,
        modified_files: tuple[str, ...],
        verification: str,
        modified_directories: tuple[str, ...] = (),
    ) -> None:
        """开始记录一个任务；同一时间只允许一个活动任务。"""

        with self._revision_lock:
            if self._active_changes is not None:
                raise ChangeJournalError("当前任务尚未封存")
            self._active_changes = {}
            self._active_after = {}
            self._active_tainted_paths = set()
            self._active_directory_changes = {}
            self._active_directory_after = {}
            self._active_tainted_directory_paths = set()
            self._active_directory_reservations = set()
            self._revision = 0
            self._before_modified_files = modified_files
            self._before_modified_directories = modified_directories
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

    def set_directory_baseline(self, paths: tuple[str, ...]) -> None:
        """兼容旧 begin_task 子类签名，单独绑定任务开始时的目录状态。"""

        with self._revision_lock:
            self._require_active_changes()
            self._before_modified_directories = paths

    def record_committed(
        self,
        path: str,
        before: FileSnapshot | None,
        after: FileSnapshot | None,
    ) -> None:
        """记录一次已提交写入，保留该路径的最早前态与最新后态。"""

        with self._revision_lock:
            changes = self._require_active_changes()
            self._validate_change(FileChange(path, before, after))
            self._apply(changes, FileChange(path, before, after))
            if self._active_after is None:
                raise ChangeJournalError("尚未开始任务变更记录")
            self._active_after[path] = after
            self._revision += 1

    def reserve_directories(self, paths: tuple[str, ...]) -> None:
        """在审批和写盘前预留本任务可能新增的规范目录路径。"""

        with self._revision_lock:
            self._require_active_changes()
            reservations = self._active_directory_reservations
            if reservations is None:
                raise ChangeJournalError("尚未开始任务变更记录")
            projected = reservations | set(paths)
            if len(projected) > self._max_directories:
                raise ChangeBudgetError("变更预算不足：预计新增目录数超过限制")
            reservations.update(paths)

    def record_directory_committed(
        self,
        path: str,
        before: DirectorySnapshot | None,
        after: DirectorySnapshot | None,
    ) -> None:
        """记录一次已经发生的目录创建或安全补偿。"""

        with self._revision_lock:
            self._require_active_changes()
            changes = self._require_active_directory_changes()
            change = DirectoryChange(path, before, after)
            self._validate_directory_change(change)
            self._apply_directory(changes, change)
            if self._active_directory_after is None:
                raise ChangeJournalError("尚未开始任务变更记录")
            self._active_directory_after[path] = after
            reservations = self._active_directory_reservations
            if reservations is None:
                raise ChangeJournalError("尚未开始任务变更记录")
            reservations.add(path)
            if len(reservations) > self._max_directories:
                raise ChangeBudgetError("变更预算不足：新增目录数超过限制")
            self._revision += 1

    def active_directory_after(
        self, path: str
    ) -> tuple[bool, DirectorySnapshot | None]:
        """返回目录路径是否由当前任务处理过及其最近可信后态。"""

        self._require_active_changes()
        if self._active_directory_after is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return path in self._active_directory_after, self._active_directory_after.get(path)

    def active_after(self, path: str) -> tuple[bool, FileSnapshot | None]:
        """返回路径是否写过，以及最近一次工具提交的已证明后态。"""

        self._require_active_changes()
        if self._active_after is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return path in self._active_after, self._active_after.get(path)

    def mark_tainted(self, path: str) -> None:
        """记录工具层无法证明归属连续性的规范路径，不保存外部快照。"""

        with self._revision_lock:
            self._require_active_changes()
            if self._active_tainted_paths is None:
                raise ChangeJournalError("尚未开始任务变更记录")
            if path not in self._active_tainted_paths:
                self._active_tainted_paths.add(path)
                self._revision += 1

    def mark_directory_tainted(self, path: str) -> None:
        """标记无法证明身份或补偿结果的目录路径。"""

        with self._revision_lock:
            self._require_active_changes()
            tainted = self._active_tainted_directory_paths
            if tainted is None:
                raise ChangeJournalError("尚未开始任务变更记录")
            if path not in tainted:
                tainted.add(path)
                self._revision += 1

    @property
    def active_revision(self) -> int:
        """任务内已提交事实的单调游标；读取/审批/reserve 不推进。"""
        with self._revision_lock:
            self._require_active_changes()
            return self._revision

    def active_effects_since(self, consumed_revision: int) -> FileEffects:
        """只有消费游标之后有新事实，才允许重放整个活动账本。"""
        with self._revision_lock:
            self._require_active_changes()
            if self._revision <= consumed_revision:
                return FileEffects(EffectState.NONE)
            effects = self.active_effects()
            if effects.state is EffectState.NONE:
                # 新提交可写回净零，但仍改变版本；路径来自真实提交，不制造 undo 净变化。
                return FileEffects(
                    EffectState.CONFIRMED,
                    tuple(self._active_after or ()),
                    tuple(self._active_directory_after or ()),
                )
            return effects

    def is_tainted(self, path: str) -> bool:
        """判断活动任务的规范路径是否已失去工具所有权证明。"""

        self._require_active_changes()
        if self._active_tainted_paths is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return path in self._active_tainted_paths

    def active_effects(self) -> FileEffects:
        """只读导出净变化与不确定状态，不封存、不暴露源码快照。"""

        with self._revision_lock:
            changes = self._require_active_changes()
            tainted = self._active_tainted_paths or set()
            directory_changes = self._require_active_directory_changes()
            tainted_directories = self._active_tainted_directory_paths or set()
            # changes 与 tainted_paths 都只由本地写入边界记录。即使某个路径
            # 已失去连续身份、不能升级为 CONFIRMED，也要作为 UNKNOWN 的已知
            # 受影响范围保留下来，供 Runtime、SQLite 与用户界面提示核对。
            paths = tuple(dict.fromkeys((*changes, *sorted(tainted))))
            directory_paths = tuple(
                dict.fromkeys((*directory_changes, *sorted(tainted_directories)))
            )
            state = (
                EffectState.UNKNOWN
                if tainted or tainted_directories
                else EffectState.CONFIRMED
                if paths or directory_paths
                else EffectState.NONE
            )
            return FileEffects(state, paths, directory_paths)

    def seal_task(
        self,
        modified_files: tuple[str, ...],
        verification: str,
        modified_directories: tuple[str, ...] | None = None,
    ) -> TaskChangeSet | None:
        """封存活动任务；没有净变化时保留此前的最近结果。"""

        with self._revision_lock:
            changes = self._require_active_changes()
            directory_changes = self._require_active_directory_changes()
            if modified_directories is None:
                after_directories = list(self._before_modified_directories)
                for change in directory_changes.values():
                    if change.after is None:
                        after_directories = [
                            path
                            for path in after_directories
                            if path != change.path
                        ]
                    elif change.path not in after_directories:
                        after_directories.append(change.path)
                modified_directories = tuple(after_directories)
            result = (
                TaskChangeSet(
                    changes=tuple(changes.values()),
                    before_modified_files=self._before_modified_files,
                    before_verification=self._before_verification,
                    after_modified_files=modified_files,
                    after_verification=verification,
                    tainted_paths=tuple(sorted(self._active_tainted_paths or ())),
                    directory_changes=tuple(directory_changes.values()),
                    before_modified_directories=self._before_modified_directories,
                    after_modified_directories=modified_directories,
                    tainted_directory_paths=tuple(
                        sorted(self._active_tainted_directory_paths or ())
                    ),
                )
                if (
                    changes
                    or self._active_tainted_paths
                    or directory_changes
                    or self._active_tainted_directory_paths
                )
                else None
            )
            self._active_changes = None
            self._active_after = None
            self._active_tainted_paths = None
            self._active_directory_changes = None
            self._active_directory_after = None
            self._active_tainted_directory_paths = None
            self._active_directory_reservations = None
            self._before_modified_files = ()
            self._before_modified_directories = ()
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

    def replace_latest(
        self,
        expected: TaskChangeSet,
        replacement: TaskChangeSet,
    ) -> None:
        """仅在最近账本仍是预期对象时替换失败补偿后的可信身份。"""

        with self._revision_lock:
            if self._active_changes is not None:
                raise ChangeJournalError("任务仍在执行，不能替换最近变更")
            if self._latest != expected:
                raise ChangeJournalError("最近变更已变化，拒绝替换补偿证据")
            self._latest = replacement

    def _require_active_changes(self) -> dict[str, FileChange]:
        if self._active_changes is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return self._active_changes

    def _require_active_directory_changes(self) -> dict[str, DirectoryChange]:
        if self._active_directory_changes is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return self._active_directory_changes

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

    def _apply_directory(
        self,
        changes: dict[str, DirectoryChange],
        change: DirectoryChange,
    ) -> None:
        existing = changes.get(change.path)
        merged = DirectoryChange(
            path=change.path,
            before=existing.before if existing is not None else change.before,
            after=change.after,
        )
        if self._is_directory_net_zero(merged):
            changes.pop(change.path, None)
        else:
            changes[change.path] = merged

    @staticmethod
    def _validate_change(change: FileChange) -> None:
        for snapshot in (change.before, change.after):
            if snapshot is not None and snapshot.path != change.path:
                raise ChangeJournalError("变更路径与快照路径不一致")

    @staticmethod
    def _validate_directory_change(change: DirectoryChange) -> None:
        for snapshot in (change.before, change.after):
            if snapshot is not None and snapshot.path != change.path:
                raise ChangeJournalError("目录变更路径与快照路径不一致")

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

    @staticmethod
    def _is_directory_net_zero(change: DirectoryChange) -> bool:
        if change.before is None and change.after is None:
            return True
        if change.before is None or change.after is None:
            return False
        return change.before == change.after
