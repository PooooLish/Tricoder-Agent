"""Coding Agent 可调用的本地工具。"""

from __future__ import annotations

import copy
import difflib
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tricoder.changes import (
    ChangeJournal,
    FileChange,
    FileIdentity,
    FileSnapshot,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
    render_change_set_diff,
)
from tricoder.models import ToolDefinition, ToolResult
from tricoder.patches import FilePatch, PatchError, apply_file_patch, parse_unified_diff
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy


Approver = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class _ToolRegistration:
    """将公开工具定义与唯一对应的本地处理器绑定。"""

    definition: ToolDefinition
    handler: Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True, slots=True)
class _PreparedFilePatch:
    """审批前完全计算好的单文件补丁，不保留待执行的源码操作。"""

    patch: FilePatch
    path: Path
    relative_path: str
    before: FileSnapshot | None
    after_content: str
    mode: int


@dataclass(frozen=True, slots=True)
class _CommittedFilePatch:
    """已经发布到目标路径、可用于安全补偿核验的真实后态。"""

    prepared: _PreparedFilePatch
    after: FileSnapshot


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


def _stat_identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(metadata.st_dev, metadata.st_ino)


def _is_windows() -> bool:
    """隔离平台分支，便于测试无法安全降级的路径。"""

    return os.name == "nt"


class _DirectoryBinding:
    """把审批后的发布操作绑定到审批前确认的父目录对象。"""

    def __init__(self, parent: Path) -> None:
        self.parent = parent

    @classmethod
    def open(cls, workspace: Path, parent: Path) -> "_DirectoryBinding":
        if _is_windows():
            return _WindowsDirectoryBinding(workspace, parent)
        if not {
            os.chmod,
            os.rename,
            os.open,
            os.link,
            os.unlink,
            os.stat,
        }.issubset(os.supports_dir_fd):
            raise PolicyError("当前平台无法建立安全目录绑定，拒绝写入")
        return _PosixDirectoryBinding(parent)

    def close(self) -> None:
        raise NotImplementedError

    def verify_parent(self, parent: Path) -> bool:
        raise NotImplementedError

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        raise NotImplementedError

    def target_exists(self, name: str) -> bool:
        raise NotImplementedError

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        raise NotImplementedError

    def replace(self, temporary_name: str, target_name: str) -> None:
        raise NotImplementedError

    def link(self, temporary_name: str, target_name: str) -> None:
        raise NotImplementedError

    def unlink(self, temporary_name: str) -> None:
        raise NotImplementedError

    def chmod(self, name: str, mode: int) -> None:
        raise NotImplementedError

    @staticmethod
    def _temporary_names(target_name: str) -> Any:
        for _attempt in range(128):
            yield f".{target_name}.{secrets.token_hex(8)}.tmp"
        raise OSError("无法分配安全的临时文件名")


class _PosixDirectoryBinding(_DirectoryBinding):
    """POSIX 目录句柄绑定；所有发布路径都相对于同一目录句柄。"""

    def __init__(self, parent: Path) -> None:
        super().__init__(parent)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._fd = os.open(parent, flags)
        except OSError as exc:
            raise PolicyError("无法建立安全目录绑定，拒绝写入") from exc
        metadata = os.fstat(self._fd)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(self._fd)
            raise PolicyError("写入目标的父路径不是目录")
        self._identity = _stat_identity(metadata)

    def close(self) -> None:
        os.close(self._fd)

    def verify_parent(self, parent: Path) -> bool:
        if parent != self.parent:
            return False
        try:
            return _stat_identity(parent.stat()) == self._identity
        except OSError:
            return False

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=self._fd)
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            metadata = os.fstat(file.fileno())
            content = file.read()
        return content, _stat_identity(metadata), metadata.st_mode

    def target_exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        for temporary_name in self._temporary_names(target_name):
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=self._fd,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as temporary:
                    temporary.write(content)
                os.chmod(
                    temporary_name,
                    stat.S_IMODE(mode),
                    dir_fd=self._fd,
                )
            except BaseException:
                try:
                    os.unlink(temporary_name, dir_fd=self._fd)
                except OSError:
                    pass
                raise
            return temporary_name
        raise OSError("无法分配安全的临时文件名")

    def replace(self, temporary_name: str, target_name: str) -> None:
        os.rename(
            temporary_name,
            target_name,
            src_dir_fd=self._fd,
            dst_dir_fd=self._fd,
        )

    def link(self, temporary_name: str, target_name: str) -> None:
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=self._fd,
            dst_dir_fd=self._fd,
            follow_symlinks=False,
        )

    def unlink(self, temporary_name: str) -> None:
        os.unlink(temporary_name, dir_fd=self._fd)

    def chmod(self, name: str, mode: int) -> None:
        os.chmod(name, mode, dir_fd=self._fd, follow_symlinks=False)


class _WindowsDirectoryBinding(_DirectoryBinding):
    """用不共享删除权限的目录句柄锁住 Windows 路径的每个目录组件。"""

    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    def __init__(self, workspace: Path, parent: Path) -> None:
        super().__init__(parent)
        if not parent.is_relative_to(workspace):
            raise PolicyError("写入目标的父目录不属于工作区")
        self._handles: list[int] = []
        try:
            for component in self._path_chain(parent):
                handle, identity = self._open_handle(component)
                self._handles.append(handle)
                if component == parent:
                    self._identity = identity
        except (OSError, AttributeError) as exc:
            self.close()
            raise PolicyError("Windows 无法建立防重命名目录绑定，拒绝写入") from exc
        if not hasattr(self, "_identity"):
            self.close()
            raise PolicyError("Windows 无法确认父目录身份，拒绝写入")

    @staticmethod
    def _path_chain(path: Path) -> list[Path]:
        parts = path.parts
        if not parts:
            return []
        current = Path(parts[0])
        chain = [current]
        for part in parts[1:]:
            current = current / part
            chain.append(current)
        return chain

    @classmethod
    def _open_handle(cls, path: Path) -> tuple[int, FileIdentity]:
        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            cls._FILE_READ_ATTRIBUTES,
            cls._FILE_SHARE_READ | cls._FILE_SHARE_WRITE,
            None,
            cls._OPEN_EXISTING,
            cls._FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _ByHandleFileInformation()
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        get_information.restype = wintypes.BOOL
        if not get_information(handle, ctypes.byref(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        identity = FileIdentity(
            information.volume_serial_number,
            (information.file_index_high << 32) | information.file_index_low,
        )
        return int(handle), identity

    @staticmethod
    def _close_handle(handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)

    def close(self) -> None:
        for handle in reversed(getattr(self, "_handles", [])):
            self._close_handle(handle)
        self._handles = []

    def verify_parent(self, parent: Path) -> bool:
        if parent != self.parent:
            return False
        try:
            handle, identity = self._open_handle(parent)
        except OSError:
            return False
        try:
            return identity == self._identity
        finally:
            self._close_handle(handle)

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        with (self.parent / name).open("r", encoding="utf-8") as file:
            metadata = os.fstat(file.fileno())
            content = file.read()
        return content, _stat_identity(metadata), metadata.st_mode

    def target_exists(self, name: str) -> bool:
        return os.path.lexists(self.parent / name)

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for temporary_name in self._temporary_names(target_name):
            path = self.parent / temporary_name
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as temporary:
                    temporary.write(content)
                os.chmod(path, stat.S_IMODE(mode))
            except BaseException:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            return temporary_name
        raise OSError("无法分配安全的临时文件名")

    def replace(self, temporary_name: str, target_name: str) -> None:
        os.replace(self.parent / temporary_name, self.parent / target_name)

    def link(self, temporary_name: str, target_name: str) -> None:
        os.link(self.parent / temporary_name, self.parent / target_name)

    def unlink(self, temporary_name: str) -> None:
        os.unlink(self.parent / temporary_name)

    def chmod(self, name: str, mode: int) -> None:
        os.chmod(self.parent / name, mode)


@dataclass(slots=True)
class ToolContext:
    """工具执行所需的策略、审批与资源限制。"""

    workspace_policy: WorkspacePolicy
    command_policy: CommandPolicy
    approver: Approver
    read_only: bool = False
    timeout: float = 30.0
    max_output_chars: int = 20_000
    change_journal: ChangeJournal | None = None


class ToolRegistry:
    """按固定名称分发工具，统一把预期错误转换为 ToolResult。"""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._registrations = (
            _ToolRegistration(
                ToolDefinition(
                    "list_files",
                    "列出工作区内指定目录的条目。",
                    self._schema({"path": {"type": "string"}}),
                ),
                self._list_files,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "read_file",
                    "读取工作区内 UTF-8 文本文件。",
                    self._schema({"path": {"type": "string"}}, ["path"]),
                ),
                self._read_file,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "search_text",
                    "在工作区目录内搜索文本。",
                    self._schema(
                        {
                            "path": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        ["query"],
                    ),
                ),
                self._search_text,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "edit_file",
                    "经审批后精确替换工作区内文件的一段文本。",
                    self._schema(
                        {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        ["path", "old_text", "new_text"],
                    ),
                ),
                self._edit_file,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "create_file",
                    "经审批后在工作区内创建新的 UTF-8 文件。",
                    self._schema(
                        {"path": {"type": "string"}, "content": {"type": "string"}},
                        ["path", "content"],
                    ),
                ),
                self._create_file,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "apply_patch",
                    "经一次审批后原子应用受限的多文件 unified diff。",
                    self._schema({"patch": {"type": "string"}}, ["patch"]),
                ),
                self._apply_patch,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "run_command",
                    "经审批后在工作区内运行受策略允许的命令。",
                    self._schema(
                        {"command": {"type": "string"}, "cwd": {"type": "string"}},
                        ["command"],
                    ),
                ),
                self._run_command,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "finish",
                    "提交本轮任务的文字总结。",
                    self._schema({"summary": {"type": "string"}}, ["summary"]),
                ),
                self._finish,
            ),
        )
    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回顺序固定且与内部注册表隔离的公开工具定义。"""
        return tuple(
            self._public_definition(registration.definition)
            for registration in self._registrations
        )

    def contains(self, name: str) -> bool:
        """判断名称是否在公开注册表中。"""
        return any(
            registration.definition.name == name
            for registration in self._registrations
        )

    def describe(self, name: str) -> ToolDefinition | None:
        """只返回注册表中静态声明的公开定义。"""
        for registration in self._registrations:
            if registration.definition.name == name:
                return self._public_definition(registration.definition)
        return None

    @staticmethod
    def _public_definition(definition: ToolDefinition) -> ToolDefinition:
        """复制嵌套 Schema，避免公开调用方修改注册表的校验依据。"""
        return ToolDefinition(
            definition.name,
            definition.description,
            copy.deepcopy(definition.parameters),
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        registration = next(
            (
                item
                for item in self._registrations
                if item.definition.name == name
            ),
            None,
        )
        if registration is None:
            return ToolResult(False, f"未知工具：{name}")
        try:
            self._validate_arguments(registration.definition.parameters, arguments)
            return registration.handler(arguments)
        except (PolicyError, OSError, UnicodeError, ValueError, TypeError) as exc:
            return ToolResult(False, str(exc))

    def preview_undo(self, change_set: TaskChangeSet) -> UndoPreview:
        """首次全量核验 after 快照，并生成不落盘的反向差异。"""

        if self.context.read_only:
            raise PolicyError("只读模式禁止撤销")
        paths = tuple(sorted(change.path for change in change_set.changes))
        _prepared, bindings, conflicts = self._open_undo_targets(change_set)
        self._close_bindings(bindings)
        if conflicts:
            raise PolicyError(f"无法撤销，文件状态冲突：{'、'.join(conflicts)}")
        return UndoPreview(render_change_set_diff(change_set, reverse=True), paths)

    def undo_change_set(self, change_set: TaskChangeSet) -> UndoExecution:
        """再次全量核验后恢复整组文件；中途失败则反向补偿已恢复目标。"""

        paths = tuple(sorted(change.path for change in change_set.changes))
        if self.context.read_only:
            return UndoExecution(False, paths, conflicts=paths)
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
                    # Windows 不能替换或删除只读目标；硬链接备份让后续补偿仍可恢复原 identity。
                    binding.chmod(
                        item.path.name,
                        item.change.after.mode | stat.S_IWUSR,
                    )
                    writable_after = self._snapshot(
                        binding,
                        item.path.name,
                        item.change.path,
                    )
                    committed[-1] = _CommittedUndo(item, writable_after, backup_name)
                restored = self._restore_before(item, binding, temporary_files)
                if backup_name is None:
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

        return UndoExecution(True, paths)

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
        for parent in sorted({item.path.parent for item in prepared}, key=str):
            bindings[parent] = _DirectoryBinding.open(workspace, parent)
        for item in prepared:
            binding = bindings[item.path.parent]
            if not binding.verify_parent(item.path.parent) or not self._undo_target_matches(
                item, binding
            ):
                conflicts.append(item.change.path)
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
                        raise PolicyError("补偿前目标不再缺失")
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
                    if current_state != entry.restored and not controlled_unlock:
                        raise PolicyError("补偿前目标不再等于受控恢复状态")

                after = item.change.after
                if after is None:
                    binding.unlink(item.path.name)
                    if binding.target_exists(item.path.name):
                        raise OSError("补偿后目标仍存在")
                else:
                    if entry.backup_name is None:
                        raise OSError("补偿缺少 after 备份")
                    current = self._snapshot(binding, item.path.name, item.change.path)
                    if current.identity == after.identity:
                        if binding.target_exists(entry.backup_name):
                            binding.unlink(entry.backup_name)
                    else:
                        binding.replace(entry.backup_name, item.path.name)
                    backup_files.remove((binding, entry.backup_name))
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
    def _schema(
        properties: dict[str, dict[str, str]],
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        """构建当前简单工具所需的统一对象 Schema。"""
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: object) -> None:
        """在进入处理器前校验当前工具 Schema 支持的基础类型。"""
        if schema.get("type") != "object" or not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")

        properties = schema["properties"]
        for name in schema["required"]:
            if name not in arguments:
                raise ValueError(f"缺少必填参数：{name}")
        if not schema["additionalProperties"]:
            extras = set(arguments) - set(properties)
            if extras:
                raise ValueError(f"不支持额外参数：{sorted(extras)[0]}")

        validators: dict[str, type[object]] = {
            "string": str,
            "integer": int,
            "boolean": bool,
        }
        for name, value in arguments.items():
            expected = properties[name]["type"]
            expected_type = validators.get(expected)
            if expected_type is None:
                raise ValueError(f"不支持的参数类型：{expected}")
            if not isinstance(value, expected_type) or (
                expected == "integer" and isinstance(value, bool)
            ):
                raise ValueError(f"参数 {name} 必须是 {expected}")

    def _list_files(self, arguments: dict[str, Any]) -> ToolResult:
        directory = self.context.workspace_policy.resolve_path(
            str(arguments.get("path", ".")),
        )
        if not directory.is_dir():
            return ToolResult(False, "list_files 的目标必须是目录")
        entries: list[str] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            try:
                self.context.workspace_policy.resolve_path(child)
            except PolicyError:
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.relative_to(self.context.workspace_policy.workspace)}{suffix}")
        return ToolResult(True, self._bounded("\n".join(entries) or "(空目录)"))

    def _read_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.context.workspace_policy.resolve_path(self._required_str(arguments, "path"))
        if not path.is_file():
            return ToolResult(False, "read_file 的目标必须是文件")
        content = path.read_text(encoding="utf-8")
        return ToolResult(True, self._bounded(content))

    def _search_text(self, arguments: dict[str, Any]) -> ToolResult:
        query = self._required_str(arguments, "query")
        root = self.context.workspace_policy.resolve_path(str(arguments.get("path", ".")))
        if not root.is_dir():
            return ToolResult(False, "search_text 的 path 必须是目录")

        matches: list[str] = []
        for current_root, directories, files in os.walk(root, followlinks=False):
            current = Path(current_root)
            # 在进入子目录前过滤敏感目录和可能逃逸的链接。
            allowed_directories: list[str] = []
            for name in directories:
                try:
                    candidate = self.context.workspace_policy.resolve_path(current / name)
                except PolicyError:
                    continue
                if not candidate.is_symlink():
                    allowed_directories.append(name)
            directories[:] = allowed_directories

            for name in files:
                try:
                    path = self.context.workspace_policy.resolve_path(current / name)
                    text = path.read_text(encoding="utf-8")
                except (PolicyError, UnicodeError, OSError):
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        relative = path.relative_to(self.context.workspace_policy.workspace)
                        matches.append(f"{relative}:{line_number}: {line.strip()}")
                        if len(matches) >= 200:
                            return ToolResult(True, self._bounded("\n".join(matches)))
        return ToolResult(True, self._bounded("\n".join(matches) or "(无匹配)"))

    def _edit_file(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止编辑文件")
        raw_path = self._required_str(arguments, "path")
        path = self.context.workspace_policy.resolve_path(raw_path)
        if not path.is_file():
            return ToolResult(False, "edit_file 的目标必须是文件")
        old_text = self._required_str(arguments, "old_text")
        new_text = self._required_str(arguments, "new_text", allow_empty=True)
        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            path.parent,
        )
        temporary_name: str | None = None
        committed = False
        close_warning = False
        journal_warning = False
        try:
            preapproved = self.context.workspace_policy.resolve_path(raw_path)
            if preapproved != path or not binding.verify_parent(preapproved.parent):
                return ToolResult(False, "目标父目录身份发生变化，拒绝请求审批")
            relative = path.relative_to(self.context.workspace_policy.workspace)
            relative_path = relative.as_posix()
            before = self._snapshot(binding, path.name, relative_path)
            original = before.content
            if original.count(old_text) != 1:
                return ToolResult(
                    False,
                    "old_text 必须在目标文件中恰好出现一次，请重新读取文件",
                )
            updated = original.replace(old_text, new_text, 1)
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=str(relative),
                    tofile=str(relative),
                )
            )
            projected_after = FileSnapshot(
                relative_path,
                updated,
                before.mode,
                before.identity,
            )
            self._reserve_change(FileChange(relative_path, before, projected_after))
            if not self.context.approver("edit_file", diff):
                return ToolResult(False, "用户拒绝了文件修改")

            verified = self.context.workspace_policy.resolve_path(raw_path)
            if not binding.verify_parent(verified.parent) or verified != path:
                return ToolResult(False, "审批后目标父目录身份发生变化，拒绝写入")
            current = self._snapshot(binding, path.name, relative_path)
            if current.identity != before.identity or current.content != original:
                return ToolResult(False, "审批后目标文件发生变化，请重新读取并审批")

            temporary_name = binding.create_temporary(
                path.name,
                updated,
                before.mode,
            )
            published_after = self._snapshot(binding, temporary_name, relative_path)
            binding.replace(temporary_name, path.name)
            committed = True
            temporary_name = None
            try:
                after = self._snapshot(binding, path.name, relative_path)
            except Exception:
                after = published_after
                journal_warning = True
            try:
                self._record_committed(relative_path, before, after)
            except Exception:
                journal_warning = True
        finally:
            if temporary_name is not None:
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            try:
                binding.close()
            except OSError:
                if not committed:
                    raise
                close_warning = True
        output = f"已修改 {relative}"
        if journal_warning:
            output += "；账本警告：提交后快照或记录失败，文件修改已提交，请在验证时检查路径"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，文件修改已提交，请在验证时检查目录"
        return ToolResult(True, output, relative_path)

    def _create_file(self, arguments: dict[str, Any]) -> ToolResult:
        """经审批后，以不可覆盖的原子发布方式创建 UTF-8 文件。"""
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止创建文件")
        raw_path = self._required_str(arguments, "path")
        path = self.context.workspace_policy.resolve_path(
            raw_path,
            must_exist=False,
        )
        content = self._required_str(arguments, "content", allow_empty=True)
        if not path.parent.is_dir():
            return ToolResult(False, "create_file 的父目录必须存在")
        if path.exists():
            return ToolResult(False, "create_file 的目标文件已存在，拒绝覆盖")

        relative = path.relative_to(self.context.workspace_policy.workspace)
        diff = (
            "".join(
                difflib.unified_diff(
                    [],
                    content.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=str(relative),
                )
            )
            if content
            else (
                f"--- /dev/null\n+++ {relative}\n"
                "@@ -0,0 +0,0 @@\n"
                "（创建空文件，内容为 0 字符）\n"
            )
        )

        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            path.parent,
        )
        temporary_name: str | None = None
        committed = False
        cleanup_warning = False
        close_warning = False
        journal_warning = False
        try:
            preapproved = self.context.workspace_policy.resolve_path(
                raw_path,
                must_exist=False,
            )
            if preapproved != path or not binding.verify_parent(preapproved.parent):
                return ToolResult(False, "目标父目录身份发生变化，拒绝请求审批")
            relative_path = relative.as_posix()
            projected_after = FileSnapshot(
                relative_path,
                content,
                0o600,
                FileIdentity(0, 0),
            )
            self._reserve_change(FileChange(relative_path, None, projected_after))
            if not self.context.approver("create_file", diff):
                return ToolResult(False, "用户拒绝了创建文件")

            verified = self.context.workspace_policy.resolve_path(
                raw_path,
                must_exist=False,
            )
            if not binding.verify_parent(verified.parent) or verified != path:
                return ToolResult(False, "审批后目标父目录身份发生变化，拒绝写入")
            if binding.target_exists(path.name):
                return ToolResult(False, "审批后目标文件已存在，拒绝覆盖")

            temporary_name = binding.create_temporary(path.name, content, 0o600)
            published_after = self._snapshot(binding, temporary_name, relative_path)
            try:
                binding.link(temporary_name, path.name)
            except OSError:
                return ToolResult(False, "create_file 无法原子发布文件，拒绝覆盖")
            committed = True
            try:
                after = self._snapshot(binding, path.name, relative_path)
            except Exception:
                after = published_after
                journal_warning = True
            try:
                self._record_committed(relative_path, None, after)
            except Exception:
                journal_warning = True
            try:
                binding.unlink(temporary_name)
            except OSError:
                cleanup_warning = True
            temporary_name = None
        finally:
            if temporary_name is not None and not committed:
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            try:
                binding.close()
            except OSError:
                if not committed:
                    raise
                close_warning = True
        output = f"已创建 {relative}"
        if journal_warning:
            output += "；账本警告：提交后快照或记录失败，文件创建已提交，请在验证时检查路径"
        if cleanup_warning:
            output += "；清理警告：临时链接未能删除，请在验证时检查目录"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，文件创建已提交，请在验证时检查目录"
        return ToolResult(True, output, relative_path)

    def _apply_patch(self, arguments: dict[str, Any]) -> ToolResult:
        """全量预检后，以一次审批提交受限的多文件补丁。"""

        if self.context.read_only:
            return ToolResult(False, "只读模式禁止应用补丁")
        source = self._required_str(arguments, "patch")
        try:
            file_patches = parse_unified_diff(source)
        except PatchError as exc:
            return ToolResult(False, f"补丁无效：{exc}")

        workspace = self.context.workspace_policy.workspace
        resolved: list[tuple[FilePatch, Path, str]] = []
        seen_targets: set[str] = set()
        for file_patch in file_patches:
            path = self.context.workspace_policy.resolve_path(
                file_patch.path,
                must_exist=not file_patch.create,
            )
            relative_path = path.relative_to(workspace).as_posix()
            if relative_path in seen_targets:
                return ToolResult(False, f"补丁目标重复：{relative_path}")
            seen_targets.add(relative_path)
            if not path.parent.is_dir():
                return ToolResult(False, f"补丁目标父目录不存在：{relative_path}")
            if file_patch.create:
                if path.exists():
                    return ToolResult(False, f"补丁创建目标已存在：{relative_path}")
            elif not path.is_file():
                return ToolResult(False, f"补丁目标不是普通文件：{relative_path}")
            resolved.append((file_patch, path, relative_path))

        bindings: dict[Path, _DirectoryBinding] = {}
        temporary_files: list[tuple[_DirectoryBinding, str]] = []
        committed_paths: list[str] = []
        close_warning = False
        try:
            for parent in sorted({path.parent for _, path, _ in resolved}, key=str):
                bindings[parent] = _DirectoryBinding.open(workspace, parent)

            prepared: list[_PreparedFilePatch] = []
            for file_patch, path, relative_path in resolved:
                binding = bindings[path.parent]
                if not binding.verify_parent(path.parent):
                    return ToolResult(False, f"补丁目标父目录身份已变化：{relative_path}")
                if file_patch.create:
                    if binding.target_exists(path.name):
                        return ToolResult(False, f"补丁创建目标已存在：{relative_path}")
                    before = None
                    original = ""
                    mode = 0o600
                else:
                    before = self._snapshot(binding, path.name, relative_path)
                    original = before.content
                    mode = before.mode
                try:
                    after_content = apply_file_patch(original, file_patch)
                except PatchError as exc:
                    return ToolResult(False, f"补丁无法应用到 {relative_path}：{exc}")
                if not file_patch.create and after_content == original:
                    return ToolResult(False, f"补丁对现有文件没有净变化：{relative_path}")
                prepared.append(
                    _PreparedFilePatch(
                        file_patch,
                        path,
                        relative_path,
                        before,
                        after_content,
                        mode,
                    )
                )

            projected = tuple(
                FileChange(
                    item.relative_path,
                    item.before,
                    FileSnapshot(
                        item.relative_path,
                        item.after_content,
                        item.mode,
                        item.before.identity if item.before else FileIdentity(0, 0),
                    ),
                )
                for item in prepared
            )
            self._reserve_changes(projected)
            approval_detail = "".join(self._render_patch_diff(item) for item in prepared)
            try:
                approved = self.context.approver("apply_patch", approval_detail)
            except (OSError, ValueError, TypeError):
                return ToolResult(False, "补丁审批失败，未执行写入")
            if not approved:
                return ToolResult(False, "用户拒绝了多文件补丁")

            for item in prepared:
                verified = self.context.workspace_policy.resolve_path(
                    item.patch.path,
                    must_exist=not item.patch.create,
                )
                binding = bindings[item.path.parent]
                if verified != item.path or not binding.verify_parent(verified.parent):
                    return ToolResult(False, "审批后补丁目标父目录身份发生变化，拒绝写入")
                if item.before is None:
                    if binding.target_exists(item.path.name):
                        return ToolResult(False, "审批后补丁创建目标已存在，拒绝覆盖")
                else:
                    current = self._snapshot(binding, item.path.name, item.relative_path)
                    if current != item.before:
                        return ToolResult(False, "审批后补丁目标发生变化，拒绝全部写入")

            committed: list[_CommittedFilePatch] = []
            failed_path = ""
            try:
                for item in sorted(prepared, key=lambda value: value.relative_path):
                    failed_path = item.relative_path
                    binding = bindings[item.path.parent]
                    temporary_name = binding.create_temporary(
                        item.path.name,
                        item.after_content,
                        item.mode,
                    )
                    temporary_files.append((binding, temporary_name))
                    expected_after = self._snapshot(
                        binding,
                        temporary_name,
                        item.relative_path,
                    )
                    if item.before is None:
                        binding.link(temporary_name, item.path.name)
                    else:
                        binding.replace(temporary_name, item.path.name)
                        temporary_files.pop()
                    committed_item = _CommittedFilePatch(item, expected_after)
                    committed.append(committed_item)
                    after = self._snapshot(binding, item.path.name, item.relative_path)
                    committed[-1] = _CommittedFilePatch(item, after)
                    self._record_committed(item.relative_path, item.before, after)
                    committed_paths.append(item.relative_path)
                    if item.before is None:
                        binding.unlink(temporary_name)
                        temporary_files.pop()
            except Exception:
                compensation_failures = self._compensate_patch_commits(
                    committed,
                    bindings,
                    temporary_files,
                )
                affected_paths = tuple(
                    dict.fromkeys(
                        [entry.prepared.relative_path for entry in committed]
                        + ([failed_path] if failed_path else [])
                    )
                )
                affected_text = "、".join(affected_paths)
                output = f"补丁提交失败；受影响路径：{affected_text}"
                if compensation_failures:
                    output += f"；补偿未完成：{'、'.join(compensation_failures)}"
                else:
                    output += "；已完成安全补偿"
                return ToolResult(
                    False,
                    output,
                    modified_paths=tuple(compensation_failures),
                )
        finally:
            for binding, temporary_name in reversed(temporary_files):
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            for binding in reversed(tuple(bindings.values())):
                try:
                    binding.close()
                except OSError:
                    if not committed_paths:
                        raise
                    close_warning = True

        paths_text = "、".join(committed_paths)
        output = f"已应用补丁到 {len(committed_paths)} 个文件：{paths_text}"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，请验证受影响路径"
        return ToolResult(True, output, modified_paths=tuple(committed_paths))

    @staticmethod
    def _render_patch_diff(item: _PreparedFilePatch) -> str:
        """为一次审批渲染完整规范 diff，不复用模型输出上限。"""

        if item.before is None and not item.after_content:
            return (
                f"--- /dev/null\n+++ {item.relative_path}\n"
                "@@ -0,0 +0,0 @@\n（创建空文件，内容为 0 字符）\n"
            )
        before_lines = (
            item.before.content.splitlines(keepends=True) if item.before is not None else []
        )
        return "".join(
            difflib.unified_diff(
                before_lines,
                item.after_content.splitlines(keepends=True),
                fromfile=item.relative_path if item.before is not None else "/dev/null",
                tofile=item.relative_path,
            )
        )

    def _compensate_patch_commits(
        self,
        committed: list[_CommittedFilePatch],
        bindings: dict[Path, _DirectoryBinding],
        temporary_files: list[tuple[_DirectoryBinding, str]],
    ) -> tuple[str, ...]:
        """逆序补偿已发布目标；身份不符时绝不覆盖并保留真实账本状态。"""

        failures: list[str] = []
        for entry in reversed(committed):
            item = entry.prepared
            binding = bindings[item.path.parent]
            try:
                current = self._snapshot(binding, item.path.name, item.relative_path)
                if current != entry.after:
                    raise PolicyError("补偿前目标状态不再匹配已提交后态")
                if item.before is None:
                    binding.unlink(item.path.name)
                    if binding.target_exists(item.path.name):
                        raise OSError("补偿后创建目标仍然存在")
                    self._record_committed(item.relative_path, entry.after, None)
                    continue

                temporary_name = binding.create_temporary(
                    item.path.name,
                    item.before.content,
                    item.before.mode,
                )
                temporary_files.append((binding, temporary_name))
                binding.replace(temporary_name, item.path.name)
                temporary_files.pop()
                restored = self._snapshot(binding, item.path.name, item.relative_path)
                if (
                    restored.content != item.before.content
                    or restored.mode != item.before.mode
                ):
                    raise OSError("补偿后的目标内容或权限不匹配原始快照")
                self._record_committed(item.relative_path, entry.after, restored)
            except Exception:
                failures.append(item.relative_path)
                self._record_actual_patch_state(entry, binding)
        return tuple(sorted(failures))

    def _record_actual_patch_state(
        self,
        entry: _CommittedFilePatch,
        binding: _DirectoryBinding,
    ) -> None:
        """补偿失败后尽力把可观测的真实净状态写回活动账本。"""

        item = entry.prepared
        try:
            actual = (
                self._snapshot(binding, item.path.name, item.relative_path)
                if binding.target_exists(item.path.name)
                else None
            )
            self._record_committed(item.relative_path, entry.after, actual)
        except Exception:
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

    def _reserve_change(self, change: FileChange) -> None:
        """有活动账本时在审批前预留字符预算。"""

        if self.context.change_journal is not None:
            self.context.change_journal.reserve((change,))

    def _reserve_changes(self, changes: tuple[FileChange, ...]) -> None:
        """有活动账本时为一次多文件审批整体预留字符预算。"""

        if self.context.change_journal is not None:
            self.context.change_journal.reserve(changes)

    def _record_committed(
        self,
        path: str,
        before: FileSnapshot | None,
        after: FileSnapshot | None,
    ) -> None:
        """仅在文件系统提交点之后记录真实快照。"""

        if self.context.change_journal is not None:
            self.context.change_journal.record_committed(path, before, after)

    def _run_command(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止执行命令")
        command = self._required_str(arguments, "command")
        args = self.context.command_policy.validate(command)
        cwd = self.context.workspace_policy.resolve_path(str(arguments.get("cwd", ".")))
        if not cwd.is_dir():
            return ToolResult(False, "命令工作目录必须是目录")
        detail = f"目录：{cwd}\n命令：{command}\n超时：{self.context.timeout:g} 秒"
        if not self.context.approver("run_command", detail):
            return ToolResult(False, "用户拒绝了命令执行")
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.context.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"命令执行超过 {self.context.timeout:g} 秒")
        output = (
            f"退出码：{completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        return ToolResult(completed.returncode == 0, self._bounded(output))

    def _finish(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(True, self._required_str(arguments, "summary"))

    def _bounded(self, text: str) -> str:
        if len(text) <= self.context.max_output_chars:
            return text
        omitted = len(text) - self.context.max_output_chars
        return f"{text[: self.context.max_output_chars]}\n...（已截断 {omitted} 个字符）"

    @staticmethod
    def _required_str(
        arguments: dict[str, Any],
        name: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ValueError(f"{name} 必须是字符串且不能为空")
        return value
