"""审批后原子发布文件的安全目录绑定实现。"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Any

from tricoder.changes import FileIdentity
from tricoder.policy import PolicyError


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
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if (
            not isinstance(nofollow, int)
            or isinstance(nofollow, bool)
            or nofollow == 0
            or not {
                os.rename,
                os.open,
                os.link,
                os.unlink,
                os.stat,
            }.issubset(os.supports_dir_fd)
            or not callable(getattr(os, "fchmod", None))
        ):
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
        flags |= getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
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
        flags |= os.O_NOFOLLOW
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
                    os.fchmod(temporary.fileno(), stat.S_IMODE(mode))
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
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=self._fd)
        try:
            os.fchmod(descriptor, stat.S_IMODE(mode))
        finally:
            os.close(descriptor)


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
