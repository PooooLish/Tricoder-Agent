"""任务变更集的撤销与失败补偿。"""

from __future__ import annotations

import stat
from dataclasses import dataclass, replace
from pathlib import Path

from tricoder.changes import (
    DirectoryChange,
    DirectorySnapshot,
    FileChange,
    FileSnapshot,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
    render_change_set_diff,
)
from tricoder.policy import PolicyError

from tricoder.tools.binding import _DirectoryBinding
from tricoder.tools import binding as _binding


class UndoConflictError(PolicyError):
    """携带已规范化冲突路径，公共消息不依赖底层异常文本。"""

    def __init__(self, conflicts: tuple[str, ...]) -> None:
        self.conflicts = tuple(sorted(set(conflicts)))
        super().__init__(f"无法撤销，文件状态冲突：{'、'.join(self.conflicts)}")


@dataclass(frozen=True, slots=True)
class _PreparedUndo:
    """已解析并绑定到工作区边界的单个撤销目标。"""

    change: FileChange
    path: Path


@dataclass(frozen=True, slots=True)
class _CommittedUndo:
    """已恢复到 before 的目标，以及可恢复原 inode 的 after 硬链接。"""

    prepared: _PreparedUndo
    restored: FileSnapshot | None
    backup_name: str | None
    publishing: FileSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _PreparedDirectoryUndo:
    """身份和内容所有权已经过预检的目录撤销目标。"""

    change: DirectoryChange
    path: Path


@dataclass(frozen=True, slots=True)
class _DirectoryUndoCompensation:
    """失败撤销补偿后的真实身份；仅完整时才能刷新最近账本。"""

    failures: tuple[str, ...]
    file_changes: tuple[FileChange, ...]
    directory_changes: tuple[DirectoryChange, ...]


class UndoExecutor:
    """对已封存的任务变更集执行全量核验、撤销与失败补偿。"""

    def __init__(self, context: object) -> None:
        self.context = context

    def preview_undo(self, change_set: TaskChangeSet) -> UndoPreview:
        """首次全量核验 after 快照，并生成不落盘的反向差异。"""

        if self.context.read_only:
            raise PolicyError("只读模式禁止撤销")
        paths = tuple(sorted(change.path for change in change_set.changes))
        directory_paths = self._directory_paths_for_removal(change_set)
        if change_set.tainted_paths or change_set.tainted_directory_paths:
            raise UndoConflictError(
                (*change_set.tainted_paths, *change_set.tainted_directory_paths)
            )
        _prepared, bindings, conflicts = self._open_undo_targets(change_set)
        self._close_bindings(bindings)
        directory_conflicts = self._directory_undo_conflicts(change_set)
        conflicts = tuple(sorted(set((*conflicts, *directory_conflicts))))
        if conflicts:
            raise UndoConflictError(conflicts)
        return UndoPreview(
            render_change_set_diff(change_set, reverse=True),
            tuple((*paths, *directory_paths)),
            directory_paths,
        )

    def undo_change_set(self, change_set: TaskChangeSet) -> UndoExecution:
        """再次全量核验后恢复整组文件；中途失败则反向补偿已恢复目标。"""

        if change_set.directory_changes:
            return self._undo_change_set_with_directories(change_set)
        paths = tuple(sorted(change.path for change in change_set.changes))
        if self.context.read_only:
            return UndoExecution(False, paths, conflicts=paths)
        if change_set.tainted_paths:
            return UndoExecution(False, paths, conflicts=change_set.tainted_paths)
        prepared, bindings, conflicts = self._open_undo_targets(change_set)
        if conflicts:
            self._close_bindings(bindings)
            return UndoExecution(False, paths, conflicts=conflicts)

        temporary_files: list[tuple[_DirectoryBinding, str]] = []
        backup_files: list[tuple[_DirectoryBinding, str]] = []
        committed: list[_CommittedUndo] = []
        try:
            for item in prepared:
                binding = bindings[item.path.parent]
                # 全量校验完成后仍在每次发布前拒绝竞态变化；若已有提交则走补偿。
                if not self._undo_target_matches(item, binding):
                    raise PolicyError("撤销写入前目标状态发生变化")
                backup_name: str | None = None
                if item.change.after is not None:
                    backup_name = next(binding._temporary_names(item.path.name))
                    binding.link(item.path.name, backup_name)
                    backup_files.append((binding, backup_name))
                    committed.append(
                        _CommittedUndo(item, item.change.after, backup_name)
                    )
                    backup_after = self._snapshot(
                        binding,
                        backup_name,
                        item.change.path,
                    )
                    if backup_after != item.change.after:
                        raise PolicyError("撤销备份不再等于 after 快照")
                    if _binding._is_windows():
                        # 只解锁已核验 identity 的硬链接；不得按可被替换的公开目标名 chmod。
                        binding.chmod(
                            backup_name,
                            item.change.after.mode | stat.S_IWUSR,
                        )
                        writable_backup = self._snapshot(
                            binding,
                            backup_name,
                            item.change.path,
                        )
                        if (
                            writable_backup.content != item.change.after.content
                            or writable_backup.identity != item.change.after.identity
                        ):
                            raise PolicyError("撤销备份 identity 在解锁时发生变化")
                        committed[-1] = _CommittedUndo(
                            item,
                            writable_backup,
                            backup_name,
                        )
                    current_after_link = self._snapshot(
                        binding,
                        item.path.name,
                        item.change.path,
                    )
                    if current_after_link != committed[-1].restored:
                        raise PolicyError("撤销目标在备份与发布之间发生变化")
                restored = self._restore_before(
                    item,
                    binding,
                    temporary_files,
                    committed,
                )
                if backup_name is None:
                    if committed and committed[-1].prepared == item:
                        committed[-1] = _CommittedUndo(item, restored, None)
                    else:
                        committed.append(_CommittedUndo(item, restored, None))
                else:
                    committed[-1] = _CommittedUndo(item, restored, backup_name)
        except Exception:
            compensation_failed = self._compensate_undo_commits(
                committed,
                bindings,
                backup_files,
            )
            return UndoExecution(
                False,
                paths,
                compensation_failed=compensation_failed,
            )
        finally:
            for binding, temporary_name in reversed(temporary_files):
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            for binding, backup_name in reversed(backup_files):
                try:
                    binding.unlink(backup_name)
                except OSError:
                    pass
            self._close_bindings(bindings)

        restored_by_path = {
            entry.prepared.change.path: entry
            for entry in committed
        }
        restored_changes = tuple(
            FileChange(
                change.path,
                change.after,
                restored_by_path[change.path].restored,
            )
            for change in sorted(change_set.changes, key=lambda item: item.path)
        )
        return UndoExecution(True, paths, _restored_changes=restored_changes)

    @staticmethod
    def _directory_paths_for_removal(
        change_set: TaskChangeSet,
    ) -> tuple[str, ...]:
        return tuple(
            change.path
            for change in sorted(
                change_set.directory_changes,
                key=lambda item: (len(item.path.split("/")), item.path),
                reverse=True,
            )
        )

    def _directory_undo_conflicts(
        self,
        change_set: TaskChangeSet,
    ) -> tuple[str, ...]:
        """全量核验目录身份及其直接内容都属于同一变更集。"""

        workspace = self.context.workspace_policy.workspace
        owned_files = {
            change.path
            for change in change_set.changes
            if change.after is not None
        }
        owned_directories = {
            change.path
            for change in change_set.directory_changes
            if change.after is not None
        }
        owned = owned_files | owned_directories
        conflicts: list[str] = []
        for change in change_set.directory_changes:
            if change.before is not None or change.after is None:
                conflicts.append(change.path)
                continue
            try:
                path = self.context.workspace_policy.resolve_path(change.path)
                if path.relative_to(workspace).as_posix() != change.path:
                    raise PolicyError("目录路径不是规范路径")
                binding = _DirectoryBinding.open(workspace, path.parent)
                try:
                    identity, mode = binding.directory_status(path.name)
                    if (
                        identity != change.after.identity
                        or mode != change.after.mode
                    ):
                        raise PolicyError("目录身份或权限已变化")
                finally:
                    binding.close()
                for child in path.iterdir():
                    relative = child.relative_to(workspace).as_posix()
                    if relative not in owned:
                        conflicts.append(relative)
            except (OSError, PolicyError, ValueError):
                conflicts.append(change.path)
        return tuple(sorted(set(conflicts)))

    def _undo_change_set_with_directories(
        self,
        change_set: TaskChangeSet,
    ) -> UndoExecution:
        """目录任务的事务撤销；失败时重建已移除目录并恢复文件后态。"""

        file_paths = tuple(sorted(change.path for change in change_set.changes))
        directory_paths = self._directory_paths_for_removal(change_set)
        paths = tuple((*file_paths, *directory_paths))
        if self.context.read_only:
            return UndoExecution(False, paths, conflicts=paths)
        conflicts = tuple(
            sorted(
                set(
                    (
                        *change_set.tainted_paths,
                        *change_set.tainted_directory_paths,
                        *self._directory_undo_conflicts(change_set),
                    )
                )
            )
        )
        prepared, bindings, file_conflicts = self._open_undo_targets(change_set)
        conflicts = tuple(sorted(set((*conflicts, *file_conflicts))))
        if conflicts:
            self._close_bindings(bindings)
            return UndoExecution(False, paths, conflicts=conflicts)

        temporary_files: list[tuple[_DirectoryBinding, str]] = []
        restored_files: list[tuple[_PreparedUndo, FileSnapshot | None]] = []
        removed_directories: list[DirectoryChange] = []
        failure_path = ""
        try:
            for item in prepared:
                failure_path = item.change.path
                binding = bindings[item.path.parent]
                if not self._undo_target_matches(item, binding):
                    raise PolicyError("撤销写入前文件状态发生变化")
                restored = self._restore_before_unbacked(
                    item,
                    binding,
                    temporary_files,
                )
                restored_files.append((item, restored))
            self._close_bindings(bindings)
            bindings = {}

            workspace = self.context.workspace_policy.workspace
            changes_by_path = {
                change.path: change for change in change_set.directory_changes
            }
            for relative in directory_paths:
                failure_path = relative
                change = changes_by_path[relative]
                assert change.after is not None
                path = workspace / Path(relative)
                binding = _DirectoryBinding.open(workspace, path.parent)
                try:
                    binding.remove_empty_directory(
                        path.name,
                        change.after.identity,
                    )
                finally:
                    binding.close()
                removed_directories.append(change)
        except BaseException:
            self._close_bindings(bindings)
            compensation = self._compensate_directory_undo(
                removed_directories,
                restored_files,
            )
            if failure_path and failure_path not in compensation.failures:
                # 失败目标本身可能仍保持 after；只有补偿无法证明时才列入失败。
                pass
            refreshed_change_set: TaskChangeSet | None = None
            if not compensation.failures:
                compensated_files = {
                    change.path: change.after
                    for change in compensation.file_changes
                }
                compensated_directories = {
                    change.path: change.after
                    for change in compensation.directory_changes
                }
                refreshed_change_set = replace(
                    change_set,
                    changes=tuple(
                        replace(
                            change,
                            after=compensated_files.get(
                                change.path,
                                change.after,
                            ),
                        )
                        for change in change_set.changes
                    ),
                    directory_changes=tuple(
                        replace(
                            change,
                            after=compensated_directories.get(
                                change.path,
                                change.after,
                            ),
                        )
                        for change in change_set.directory_changes
                    ),
                )
            return UndoExecution(
                False,
                paths,
                compensation_failed=compensation.failures,
                _compensated_change_set=refreshed_change_set,
            )
        finally:
            for binding, temporary_name in reversed(temporary_files):
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass

        restored_changes = tuple(
            FileChange(item.change.path, item.change.after, restored)
            for item, restored in restored_files
        )
        restored_directory_changes = tuple(
            DirectoryChange(change.path, change.after, None)
            for change in removed_directories
        )
        return UndoExecution(
            True,
            paths,
            _restored_changes=restored_changes,
            _restored_directory_changes=restored_directory_changes,
        )

    def _restore_before_unbacked(
        self,
        item: _PreparedUndo,
        binding: _DirectoryBinding,
        temporary_files: list[tuple[_DirectoryBinding, str]],
    ) -> FileSnapshot | None:
        """目录事务内恢复文件 before；补偿使用账本正文而非递归删除。"""

        before = item.change.before
        if before is None:
            binding.unlink(item.path.name)
            if binding.target_exists(item.path.name):
                raise OSError("撤销后任务创建文件仍存在")
            return None
        temporary_name = binding.create_temporary(
            item.path.name,
            before.content,
            before.mode,
        )
        temporary_files.append((binding, temporary_name))
        if item.change.after is None:
            binding.link(temporary_name, item.path.name)
            binding.unlink(temporary_name)
        else:
            binding.replace(temporary_name, item.path.name)
        temporary_files.pop()
        restored = self._snapshot(binding, item.path.name, item.change.path)
        if restored.content != before.content or restored.mode != before.mode:
            raise OSError("撤销后的文件不匹配 before 快照")
        return restored

    def _compensate_directory_undo(
        self,
        removed_directories: list[DirectoryChange],
        restored_files: list[tuple[_PreparedUndo, FileSnapshot | None]],
    ) -> _DirectoryUndoCompensation:
        """按浅到深重建目录，再仅在受控恢复态上重建文件 after。"""

        workspace = self.context.workspace_policy.workspace
        failures: list[str] = []
        directory_changes: list[DirectoryChange] = []
        for change in reversed(removed_directories):
            path = workspace / Path(change.path)
            binding = None
            try:
                binding = _DirectoryBinding.open(workspace, path.parent)
                identity = binding.create_directory(path.name)
                verified, mode = binding.directory_status(path.name)
                if identity != verified:
                    raise PolicyError("目录补偿身份核验失败")
                actual = DirectorySnapshot(change.path, mode, identity)
                directory_changes.append(
                    DirectoryChange(change.path, change.after, actual)
                )
            except BaseException:
                failures.append(change.path)
            finally:
                if binding is not None:
                    try:
                        binding.close()
                    except OSError:
                        if change.path not in failures:
                            failures.append(change.path)

        file_changes: list[FileChange] = []
        for item, restored in reversed(restored_files):
            after = item.change.after
            binding = None
            temporary_name: str | None = None
            try:
                binding = _DirectoryBinding.open(workspace, item.path.parent)
                exists = binding.target_exists(item.path.name)
                if restored is None:
                    if exists:
                        raise PolicyError("文件补偿前目标不再缺失")
                else:
                    if not exists or self._snapshot(
                        binding, item.path.name, item.change.path
                    ) != restored:
                        raise PolicyError("文件补偿前目标不再等于受控恢复态")
                if after is None:
                    if exists:
                        binding.unlink(item.path.name)
                    actual = None
                else:
                    temporary_name = binding.create_temporary(
                        item.path.name,
                        after.content,
                        after.mode,
                    )
                    if exists:
                        binding.replace(temporary_name, item.path.name)
                    else:
                        binding.link(temporary_name, item.path.name)
                        binding.unlink(temporary_name)
                    temporary_name = None
                    actual = self._snapshot(
                        binding, item.path.name, item.change.path
                    )
                    if actual.content != after.content or actual.mode != after.mode:
                        raise OSError("文件补偿后内容或权限不匹配")
                file_changes.append(
                    FileChange(item.change.path, item.change.after, actual)
                )
            except BaseException:
                failures.append(item.change.path)
            finally:
                if binding is not None:
                    if temporary_name is not None:
                        try:
                            binding.unlink(temporary_name)
                        except OSError:
                            pass
                    try:
                        binding.close()
                    except OSError:
                        if item.change.path not in failures:
                            failures.append(item.change.path)
        return _DirectoryUndoCompensation(
            tuple(sorted(set(failures))),
            tuple(file_changes),
            tuple(directory_changes),
        )

    def _open_undo_targets(
        self,
        change_set: TaskChangeSet,
    ) -> tuple[list[_PreparedUndo], dict[Path, _DirectoryBinding], tuple[str, ...]]:
        """解析全部规范路径、绑定父目录，并以真实 content/mode/identity 核验 after。"""

        workspace = self.context.workspace_policy.workspace
        prepared: list[_PreparedUndo] = []
        conflicts: list[str] = []
        for change in sorted(change_set.changes, key=lambda item: item.path):
            path = self.context.workspace_policy.resolve_path(change.path, must_exist=False)
            if path.relative_to(workspace).as_posix() != change.path or not path.parent.is_dir():
                conflicts.append(change.path)
                continue
            prepared.append(_PreparedUndo(change, path))

        bindings: dict[Path, _DirectoryBinding] = {}
        try:
            for parent in sorted({item.path.parent for item in prepared}, key=str):
                bindings[parent] = _DirectoryBinding.open(workspace, parent)
            for item in prepared:
                binding = bindings[item.path.parent]
                if not binding.verify_parent(
                    item.path.parent
                ) or not self._undo_target_matches(item, binding):
                    conflicts.append(item.change.path)
        except Exception:
            self._close_bindings(bindings)
            raise
        return prepared, bindings, tuple(sorted(set(conflicts)))

    def _undo_target_matches(
        self,
        item: _PreparedUndo,
        binding: _DirectoryBinding,
    ) -> bool:
        """存在性及完整 after 快照必须同时匹配。"""

        after = item.change.after
        exists = binding.target_exists(item.path.name)
        if after is None:
            return not exists
        if not exists:
            return False
        try:
            return self._snapshot(binding, item.path.name, item.change.path) == after
        except (OSError, UnicodeError):
            return False

    def _restore_before(
        self,
        item: _PreparedUndo,
        binding: _DirectoryBinding,
        temporary_files: list[tuple[_DirectoryBinding, str]],
        committed: list[_CommittedUndo],
    ) -> FileSnapshot | None:
        """发布 before 内容；调用方已为存在的 after 保留同 inode 备份。"""

        before = item.change.before
        if before is None:
            binding.unlink(item.path.name)
            if binding.target_exists(item.path.name):
                raise OSError("撤销后任务创建文件仍存在")
            return None

        temporary_name = binding.create_temporary(
            item.path.name,
            before.content,
            before.mode,
        )
        temporary_files.append((binding, temporary_name))
        publishing = self._snapshot(binding, temporary_name, item.change.path)
        if publishing.content != before.content or publishing.mode != before.mode:
            raise OSError("撤销临时文件不匹配 before 快照")
        if committed and committed[-1].prepared == item:
            entry = committed[-1]
            committed[-1] = _CommittedUndo(
                item,
                entry.restored,
                entry.backup_name,
                publishing,
            )
        else:
            committed.append(_CommittedUndo(item, None, None, publishing))
        if item.change.after is None:
            binding.link(temporary_name, item.path.name)
            binding.unlink(temporary_name)
            temporary_files.pop()
        else:
            binding.replace(temporary_name, item.path.name)
            temporary_files.pop()
        restored = self._snapshot(binding, item.path.name, item.change.path)
        if restored.content != before.content or restored.mode != before.mode:
            raise OSError("撤销后的内容或权限不匹配 before 快照")
        return restored

    def _compensate_undo_commits(
        self,
        committed: list[_CommittedUndo],
        bindings: dict[Path, _DirectoryBinding],
        backup_files: list[tuple[_DirectoryBinding, str]],
    ) -> tuple[str, ...]:
        """仅在目标仍等于刚恢复状态时，用硬链接备份精确恢复 after 身份。"""

        failures: list[str] = []
        for entry in reversed(committed):
            item = entry.prepared
            binding = bindings[item.path.parent]
            try:
                if entry.restored is None:
                    if binding.target_exists(item.path.name):
                        current_state = self._snapshot(
                            binding,
                            item.path.name,
                            item.change.path,
                        )
                        if (
                            entry.publishing is None
                            or current_state != entry.publishing
                        ):
                            raise PolicyError("补偿前目标不再缺失或受控发布状态")
                else:
                    current_state = self._snapshot(
                        binding,
                        item.path.name,
                        item.change.path,
                    )
                    controlled_unlock = (
                        entry.backup_name is not None
                        and item.change.after is not None
                        and entry.restored == item.change.after
                        and current_state.content == item.change.after.content
                        and current_state.identity == item.change.after.identity
                    )
                    published_before = (
                        entry.publishing is not None
                        and current_state == entry.publishing
                    )
                    if (
                        current_state != entry.restored
                        and not controlled_unlock
                        and not published_before
                    ):
                        raise PolicyError("补偿前目标不再等于受控恢复状态")

                after = item.change.after
                if after is None:
                    if binding.target_exists(item.path.name):
                        binding.unlink(item.path.name)
                    if binding.target_exists(item.path.name):
                        raise OSError("补偿后目标仍存在")
                else:
                    if entry.backup_name is None:
                        raise OSError("补偿缺少 after 备份")
                    if not binding.target_exists(item.path.name):
                        binding.replace(entry.backup_name, item.path.name)
                    else:
                        current = self._snapshot(
                            binding,
                            item.path.name,
                            item.change.path,
                        )
                        if current.identity == after.identity:
                            if binding.target_exists(entry.backup_name):
                                binding.unlink(entry.backup_name)
                        else:
                            binding.replace(entry.backup_name, item.path.name)
                    backup_files.remove((binding, entry.backup_name))
                    if _binding._is_windows():
                        binding.chmod(item.path.name, after.mode)
                    if self._snapshot(binding, item.path.name, item.change.path) != after:
                        raise OSError("补偿后的目标不等于 after 快照")
            except Exception:
                failures.append(item.change.path)
        return tuple(sorted(failures))

    @staticmethod
    def _close_bindings(bindings: dict[Path, _DirectoryBinding]) -> None:
        for binding in reversed(tuple(bindings.values())):
            try:
                binding.close()
            except OSError:
                pass

    @staticmethod
    def _snapshot(
        binding: _DirectoryBinding,
        name: str,
        relative: str,
    ) -> FileSnapshot:
        """通过已绑定目录读取 UTF-8 内容、规范权限与真实文件身份。"""

        content, identity, mode = binding.read_text(name)
        return FileSnapshot(relative, content, stat.S_IMODE(mode), identity)
