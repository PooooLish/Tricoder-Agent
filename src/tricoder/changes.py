"""任务内文件变更的纯内存日志。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
import re


MAX_TASK_CHANGE_CHARS = 2_000_000
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
class TaskChangeSet:
    """已封存任务的变更与验证元数据。"""

    changes: tuple[FileChange, ...]
    before_modified_files: tuple[str, ...]
    before_verification: str
    after_modified_files: tuple[str, ...]
    after_verification: str
    tainted_paths: tuple[str, ...] = ()


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

    if change_set.tainted_paths:
        raise ChangeJournalError(
            f"任务文件状态冲突：{'、'.join(change_set.tainted_paths)}"
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

    def __init__(self, max_chars: int = MAX_TASK_CHANGE_CHARS) -> None:
        self._max_chars = max_chars
        self._active_changes: dict[str, FileChange] | None = None
        self._active_after: dict[str, FileSnapshot | None] | None = None
        self._active_tainted_paths: set[str] | None = None
        self._before_modified_files: tuple[str, ...] = ()
        self._before_verification = ""
        self._latest: TaskChangeSet | None = None

    def begin_task(self, modified_files: tuple[str, ...], verification: str) -> None:
        """开始记录一个任务；同一时间只允许一个活动任务。"""

        if self._active_changes is not None:
            raise ChangeJournalError("当前任务尚未封存")
        self._active_changes = {}
        self._active_after = {}
        self._active_tainted_paths = set()
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
        if self._active_after is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        self._active_after[path] = after

    def active_after(self, path: str) -> tuple[bool, FileSnapshot | None]:
        """返回路径是否写过，以及最近一次工具提交的已证明后态。"""

        self._require_active_changes()
        if self._active_after is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return path in self._active_after, self._active_after.get(path)

    def mark_tainted(self, path: str) -> None:
        """记录工具层无法证明归属连续性的规范路径，不保存外部快照。"""

        self._require_active_changes()
        if self._active_tainted_paths is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        self._active_tainted_paths.add(path)

    def is_tainted(self, path: str) -> bool:
        """判断活动任务的规范路径是否已失去工具所有权证明。"""

        self._require_active_changes()
        if self._active_tainted_paths is None:
            raise ChangeJournalError("尚未开始任务变更记录")
        return path in self._active_tainted_paths

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
                tainted_paths=tuple(sorted(self._active_tainted_paths or ())),
            )
            if changes
            else None
        )
        self._active_changes = None
        self._active_after = None
        self._active_tainted_paths = None
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
