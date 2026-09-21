"""Docker 工作副本的精确预览、确认发布和独立发布撤销。"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
from pathlib import Path, PurePosixPath
import stat
from typing import Literal

from tricoder.changes import ChangeJournal, TaskChangeSet, UndoExecution, UndoPreview
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.sandbox.workspace import (
    SandboxWorkspace,
    SandboxWorkspaceError,
    WorkspaceEntry,
)
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.binding import _DirectoryBinding, preserve_newlines


_MAX_PUBLISH_CHARS = 2_000_000


class SandboxPublishError(RuntimeError):
    """发布预览过期、越界或包含首版不支持的变更。"""


@dataclass(frozen=True, slots=True)
class _PublishFile:
    path: str
    before: str | None = field(repr=False)
    after: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PublishPreview:
    """绑定 Session/generation、完整副本版本和原文件前态的预览。"""

    session_id: str
    generation: int
    diff: str
    paths: tuple[str, ...]
    files: tuple[_PublishFile, ...] = field(repr=False)
    execution_entries: dict[str, WorkspaceEntry] = field(repr=False, compare=False)
    original_entries: dict[str, WorkspaceEntry | None] = field(repr=False, compare=False)
    _authority: object = field(repr=False, compare=False, default=None)


@dataclass(frozen=True, slots=True)
class PublishUndoPreview:
    revision: int
    diff: str
    paths: tuple[str, ...]
    _authority: object = field(repr=False, compare=False, default=None)


@dataclass(frozen=True, slots=True)
class PublishExecution:
    ok: bool
    paths: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    compensation_failed: tuple[str, ...] = ()


class SandboxPublisher:
    """只向绑定的原项目发布；副本修改与发布撤销使用不同账本。"""

    def __init__(self, sandbox: SandboxWorkspace) -> None:
        if sandbox.adopted:
            raise SandboxPublishError("Eval 临时工作区不支持发布到项目")
        self.sandbox = sandbox
        self._authority = object()
        self._latest: TaskChangeSet | None = None
        self._revision = 0
        self._uncertain = False

    @property
    def uncertain(self) -> bool:
        return self._uncertain

    @property
    def has_pending_changes(self) -> bool:
        return bool(self.sandbox.changed_paths())

    def prepare(self) -> PublishPreview:
        """稳定读取完整变更集；任何删除、类型或非文本变更拒绝整批。"""

        with self.sandbox.operation_lock:
            self._require_usable()
            execution = self.sandbox.current_entries()
            original = self.sandbox.original_entries()
            changes = self.sandbox.changed_paths()
            if not changes:
                raise SandboxPublishError("执行副本没有待发布变更")
            unsupported = tuple(path for kind, path in changes if kind in {"D", "T"})
            if unsupported:
                label = "删除或类型变化"
                raise SandboxPublishError(f"首版不支持{label}：{'、'.join(unsupported)}")

            files: list[_PublishFile] = []
            original_versions: dict[str, WorkspaceEntry | None] = {}
            total_chars = 0
            for kind, relative in changes:
                after_entry = execution.get(relative)
                if after_entry is None or after_entry.kind != "file":
                    raise SandboxPublishError(f"待发布目标不是普通文件：{relative}")
                baseline_entry = self.sandbox.baseline.get(relative)
                current_original = original.get(relative)
                if kind == "M":
                    if baseline_entry is None or baseline_entry.kind != "file":
                        raise SandboxPublishError(f"发布基线无效：{relative}")
                    if current_original != baseline_entry:
                        raise SandboxPublishError(f"原项目文件冲突：{relative}")
                    if after_entry.mode != baseline_entry.mode:
                        raise SandboxPublishError(f"首版不支持权限变化：{relative}")
                    before = _read_text(
                        self.sandbox.original_workspace,
                        relative,
                        baseline_entry,
                    )
                elif kind == "A":
                    if current_original is not None:
                        raise SandboxPublishError(f"创建目标已存在：{relative}")
                    parent = PurePosixPath(relative).parent.as_posix()
                    if parent != "." and (
                        original.get(parent) is None
                        or original[parent].kind != "directory"
                    ):
                        raise SandboxPublishError(f"首版要求创建目标父目录已存在：{relative}")
                    before = None
                else:
                    raise SandboxPublishError(f"未知发布变更：{relative}")
                after = _read_text(
                    self.sandbox.execution_workspace,
                    relative,
                    after_entry,
                )
                if "\x00" in after or (before is not None and "\x00" in before):
                    raise SandboxPublishError(f"首版不支持二进制文本：{relative}")
                total_chars += len(after) + (len(before) if before is not None else 0)
                if total_chars > _MAX_PUBLISH_CHARS:
                    raise SandboxPublishError("待发布文本超过内存预算")
                files.append(_PublishFile(relative, before, after))
                original_versions[relative] = current_original

            patch = "".join(_render_patch(item) for item in files)
            return PublishPreview(
                session_id=self.sandbox.session_id,
                generation=self.sandbox.generation,
                diff=patch,
                paths=tuple(item.path for item in files),
                files=tuple(files),
                execution_entries=execution,
                original_entries=original_versions,
                _authority=self._authority,
            )

    def apply(self, preview: PublishPreview) -> PublishExecution:
        """确认后重验全部前态，再复用受控补丁事务写回原项目。"""

        with self.sandbox.operation_lock:
            self._require_usable()
            self._validate_preview(preview)
            if self.sandbox.current_entries() != preview.execution_entries:
                raise SandboxPublishError("发布预览已过期：执行副本发生变化")
            current_original = self.sandbox.original_entries()
            conflicts = tuple(
                path
                for path, expected in preview.original_entries.items()
                if current_original.get(path) != expected
            )
            if conflicts:
                raise SandboxPublishError(f"原项目文件冲突：{'、'.join(conflicts)}")

            journal = ChangeJournal()
            journal.begin_task((), "未运行")
            registry = self._registry(journal)
            with preserve_newlines():
                try:
                    result = registry.execute("apply_patch", {"patch": preview.diff})
                finally:
                    change_set = journal.seal_task((), "待验证")
            if not result.ok:
                compensation = tuple(result.modified_paths)
                if compensation:
                    self._uncertain = True
                return PublishExecution(
                    False,
                    preview.paths,
                    conflicts=(() if compensation else preview.paths),
                    compensation_failed=compensation,
                )
            if change_set is None or change_set.tainted_paths:
                self._uncertain = True
                return PublishExecution(
                    False,
                    preview.paths,
                    compensation_failed=preview.paths,
                )
            try:
                self.sandbox.rebase_from_original(require_execution_match=True)
            except (SandboxWorkspaceError, OSError):
                with preserve_newlines():
                    compensated = registry.undo_change_set(change_set)
                if not compensated.ok:
                    self._uncertain = True
                    return PublishExecution(
                        False,
                        preview.paths,
                        compensation_failed=(
                            compensated.compensation_failed or preview.paths
                        ),
                    )
                return PublishExecution(False, preview.paths)
            self._latest = change_set
            self._revision += 1
            return PublishExecution(True, preview.paths)

    def prepare_undo(self) -> PublishUndoPreview:
        """发布撤销只针对原项目写回，不触碰副本内工具撤销账本。"""

        with self.sandbox.operation_lock:
            self._require_usable()
            if self._latest is None:
                raise SandboxPublishError("没有可撤销的最近发布")
            with preserve_newlines():
                preview = self._registry(ChangeJournal()).preview_undo(self._latest)
            return PublishUndoPreview(
                self._revision,
                preview.diff,
                preview.paths,
                self._authority,
            )

    def undo(self, preview: PublishUndoPreview) -> PublishExecution:
        with self.sandbox.operation_lock:
            self._require_usable()
            if (
                type(preview) is not PublishUndoPreview
                or preview._authority is not self._authority
                or preview.revision != self._revision
                or self._latest is None
            ):
                raise SandboxPublishError("发布撤销预览已过期")
            with preserve_newlines():
                execution = self._registry(ChangeJournal()).undo_change_set(self._latest)
            if not execution.ok:
                if execution.compensation_failed:
                    self._uncertain = True
                return PublishExecution(
                    False,
                    execution.paths,
                    conflicts=execution.conflicts,
                    compensation_failed=execution.compensation_failed,
                )
            try:
                self.sandbox.rebase_from_original(require_execution_match=False)
            except (SandboxWorkspaceError, OSError):
                self._uncertain = True
                return PublishExecution(
                    False,
                    execution.paths,
                    compensation_failed=execution.paths,
                )
            self._latest = None
            self._revision += 1
            return PublishExecution(True, execution.paths)

    def _validate_preview(self, preview: PublishPreview) -> None:
        if (
            type(preview) is not PublishPreview
            or preview._authority is not self._authority
            or preview.session_id != self.sandbox.session_id
            or preview.generation != self.sandbox.generation
        ):
            raise SandboxPublishError("发布预览不属于当前 Session/generation")

    def _registry(self, journal: ChangeJournal) -> ToolRegistry:
        workspace = self.sandbox.original_workspace
        return ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(workspace),
                command_policy=CommandPolicy(workspace),
                approver=lambda _action, _detail: True,
                change_journal=journal,
            )
        )

    def _require_usable(self) -> None:
        if self._uncertain:
            raise SandboxPublishError("发布状态不确定，禁止继续自动操作")


def _read_text(root: Path, relative: str, expected: WorkspaceEntry) -> str:
    path = root / Path(*PurePosixPath(relative).parts)
    binding = _DirectoryBinding.open(root, path.parent)
    try:
        if not binding.verify_parent(path.parent) or not binding.target_exists(path.name):
            raise SandboxPublishError(f"文件版本读取冲突：{relative}")
        try:
            with preserve_newlines():
                content, identity, mode = binding.read_text(path.name)
        except UnicodeError as exc:
            raise SandboxPublishError(f"文件不是有效 UTF-8 文本：{relative}") from exc
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            identity.device != expected.device
            or identity.inode != expected.inode
            or stat.S_IMODE(mode) != expected.mode
            or len(content.encode("utf-8")) != expected.size
            or digest != expected.digest
        ):
            raise SandboxPublishError(f"文件版本读取冲突：{relative}")
        return content
    except OSError as exc:
        raise SandboxPublishError(f"无法安全读取待发布文件：{relative}") from exc
    finally:
        binding.close()


def _render_patch(item: _PublishFile) -> str:
    before_lines = [] if item.before is None else item.before.splitlines(keepends=True)
    after_lines = item.after.splitlines(keepends=True)
    rendered = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="/dev/null" if item.before is None else f"a/{item.path}",
            tofile=f"b/{item.path}",
        )
    )
    if item.before is None and item.after == "":
        return f"--- /dev/null\n+++ b/{item.path}\n@@ -0,0 +0,0 @@\n"
    if not rendered:
        raise SandboxPublishError(f"待发布文件没有文本净变化：{item.path}")
    return rendered
