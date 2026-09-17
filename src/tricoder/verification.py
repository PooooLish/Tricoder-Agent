"""仅驻内存的受覆盖工作区证据，不等同于业务验收或对抗性沙箱。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from tricoder.policy import WorkspacePolicy, is_sensitive_workspace_path


_SCOPE_VERSION = "workspace-v1:git,venv,pycache,pytest-cache;owned-audit-files"
_CACHE_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", ".pytest_cache"})
_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    scope_id: str = field(repr=False)
    digest: str = field(repr=False)
    complete: bool
    # 只公开覆盖限制类别，不泄漏路径、源码或逐文件 hash。
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    task_id: str
    command_id: str
    before: WorkspaceSnapshot
    after: WorkspaceSnapshot
    passed: bool
    _authority: object | None = field(default=None, repr=False, compare=False)

    def is_valid_for(self, current: WorkspaceSnapshot) -> bool:
        return self.passed and stable_snapshots(self.before, self.after, current)


def stable_snapshots(*snapshots: WorkspaceSnapshot) -> bool:
    return bool(snapshots) and all(
        type(item) is WorkspaceSnapshot and item.complete
        and item.scope_id == snapshots[0].scope_id and item.digest == snapshots[0].digest
        for item in snapshots
    )


def proves_new_file_version(previous: WorkspaceSnapshot, current: WorkspaceSnapshot) -> bool:
    """只有两个本地完整且同范围的快照可证明新版本；不可比较不等于变化。"""
    return (type(previous) is WorkspaceSnapshot and type(current) is WorkspaceSnapshot
            and previous.complete and current.complete
            and previous.scope_id == current.scope_id and previous.digest != current.digest)


@dataclass(slots=True)
class VerificationScope:
    """每个本地 ToolContext 独立的能力令牌；不接收模型的 Session/task ID。"""

    token: str = field(default_factory=lambda: secrets.token_hex(24), repr=False)
    task_id: str = field(default_factory=lambda: secrets.token_hex(16))
    audit_files: tuple[Path, ...] = field(default=(), repr=False)
    _authority: object = field(default_factory=object, repr=False)
    unknown_effects: bool = False

    def begin_task(self) -> None:
        self.task_id = secrets.token_hex(16)

    def revoke(self) -> None:
        self._authority = object()

    def capture(self, policy: WorkspacePolicy) -> WorkspaceSnapshot:
        return capture_workspace(policy, scope_id=self.token, _audit_files=self.audit_files)

    def issue(self, before: WorkspaceSnapshot, after: WorkspaceSnapshot, passed: bool) -> VerificationEvidence:
        return VerificationEvidence(self.task_id, secrets.token_hex(16), before, after,
                                    passed and stable_snapshots(before, after), self._authority)

    def owns(self, evidence: VerificationEvidence | None) -> bool:
        return type(evidence) is VerificationEvidence and evidence._authority is self._authority


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _metadata(metadata: os.stat_result) -> tuple[int, ...]:
    """返回跨 Python/文件系统稳定、且足以绑定对象版本的元数据。

    目录大小和链接数会因被排除的 ``__pycache__`` 等目录出现而变化；若把
    它们用于两次清单核验，验证命令自身就会令快照假失败。文件时间戳在
    Windows 的 path/handle API 及 Python 版本间也并不稳定。目录仅绑定类型
    和身份，普通文件再加入权限、大小、链接数和 Windows 属性；文件正文由
    后续 SHA-256 覆盖。
    """
    identity = (stat.S_IFMT(metadata.st_mode), metadata.st_dev, metadata.st_ino)
    if stat.S_ISDIR(metadata.st_mode):
        return identity
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


@contextmanager
def _bound_directory(root: Path, directory: Path) -> Iterator[int | None]:
    if os.name == "nt":
        # 复用写工具的防重命名目录链绑定，不引入模块加载时的 tools 循环依赖。
        from tricoder.tools.binding import _WindowsDirectoryBinding

        class ScanBinding(_WindowsDirectoryBinding):
            _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000 | 0x00200000  # OPEN_REPARSE_POINT

            @classmethod
            def _open_handle(cls, path):
                before = path.lstat()
                if _is_reparse(before) or not stat.S_ISDIR(before.st_mode):
                    raise _IncompleteScan("link-or-type")
                handle, identity = super()._open_handle(path)
                try:
                    if _metadata(before) != _metadata(path.lstat()):
                        raise _IncompleteScan("race")
                except BaseException:
                    cls._close_handle(handle)
                    raise
                return handle, identity

        binding = ScanBinding(root, directory)
        try:
            yield None
        finally:
            binding.close()
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptors = []
    try:
        # 每个路径组件均 NOFOLLOW；不能只锁末尾父目录后跟随中间 symlink。
        descriptor = os.open(root.anchor, flags)
        descriptors.append(descriptor)
        for part in directory.parts[1:]:
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_binary(path: Path, *, dir_fd: int | None = None):
    if os.name != "nt":
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        return os.fdopen(os.open(path.name, flags, dir_fd=dir_fd), "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    # 不跟随最终 reparse，不共享写入或删除；读取前仍核对句柄身份与类型。
    handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close(handle)
        raise
    return os.fdopen(descriptor, "rb")


class _IncompleteScan(OSError):
    pass


def capture_workspace(
    policy: WorkspacePolicy, *, scope_id: str, max_files: int = 10000,
    max_total_bytes: int = 104857600, max_file_bytes: int = 8388608,
    timeout: float = 3.0, _audit_files: tuple[Path, ...] = (),
) -> WorkspaceSnapshot:
    """两次清单核验 + 分块读取；任何不完整均失败，不重试成乐观快照。

    限时为协作式预算（系统调用可能阻塞）；快照返回后仍有 TOCTOU 窗口。
    """
    root = policy.workspace
    excluded = tuple(sorted(str(path.absolute()) for path in _audit_files))
    scope = hashlib.sha256(json.dumps(
        [_SCOPE_VERSION, os.path.normcase(str(root)), scope_id, excluded],
        ensure_ascii=True, separators=(",", ":"),
    ).encode()).hexdigest()
    digest = hashlib.sha256()
    limitations: set[str] = set()
    started = time.monotonic()

    def check_budget():
        if time.monotonic() - started >= timeout:
            raise _IncompleteScan("timeout")

    def inventory():
        entries: dict[str, tuple[int, ...]] = {}
        pending = [root]
        total_bytes = 0
        count = 0
        while pending:
            check_budget()
            directory = pending.pop()
            metadata = directory.lstat()
            if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise _IncompleteScan("link-or-type")
            relative_dir = directory.relative_to(root).as_posix()
            if relative_dir in entries and entries[relative_dir] != _metadata(metadata):
                raise _IncompleteScan("race")
            entries[relative_dir] = _metadata(metadata)
            with _bound_directory(root, directory) as descriptor:
                # 目录对象必须仍是清单里同一个对象；POSIX 用句柄而非可替换路径。
                bound = os.fstat(descriptor) if descriptor is not None else directory.lstat()
                if _metadata(bound) != _metadata(metadata):
                    raise _IncompleteScan("race")
                with os.scandir(descriptor if descriptor is not None else directory) as iterator:
                    for entry in iterator:
                        check_budget()
                        path = directory / entry.name
                        relative = path.relative_to(root).as_posix()
                        item = (os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                                if descriptor is not None else path.lstat())
                        if _is_reparse(item):
                            raise _IncompleteScan("link-or-type")
                        if stat.S_ISDIR(item.st_mode) and entry.name.lower() in _CACHE_DIRECTORIES:
                            continue
                        if str(path.absolute()) in excluded and stat.S_ISREG(item.st_mode):
                            continue
                        if is_sensitive_workspace_path(relative):
                            limitations.add("sensitive")
                            continue
                        count += 1
                        # 目录也计入数目预算，防止大量空目录形成无界扫描。
                        if count > max_files:
                            raise _IncompleteScan("file-count")
                        entries[relative] = _metadata(item)
                        if stat.S_ISDIR(item.st_mode):
                            pending.append(path)
                        elif stat.S_ISREG(item.st_mode):
                            total_bytes += item.st_size
                            if item.st_size > max_file_bytes:
                                raise _IncompleteScan("file-size")
                            if total_bytes > max_total_bytes:
                                raise _IncompleteScan("total-bytes")
                        else:
                            raise _IncompleteScan("link-or-type")
        return entries

    try:
        before = inventory()
        read_bytes = 0
        for relative, expected in sorted(before.items()):
            check_budget()
            if stat.S_ISDIR(expected[0]):
                # 跨快照忽略目录时间/大小，缓存新增不改变受覆盖文件版本。
                digest.update(json.dumps([relative, expected[:3]], separators=(",", ":")).encode())
                continue
            path = root / relative
            digest.update(json.dumps([relative, expected], separators=(",", ":")).encode())
            with _bound_directory(root, path.parent) as descriptor:
                with _open_binary(path, dir_fd=descriptor) as source:
                    actual = os.fstat(source.fileno())
                    if _is_reparse(actual) or not stat.S_ISREG(actual.st_mode) or _metadata(actual) != expected:
                        raise _IncompleteScan("race")
                    size = 0
                    content = hashlib.sha256()
                    while True:
                        check_budget()
                        block = source.read(_CHUNK)
                        if not block:
                            break
                        size += len(block)
                        read_bytes += len(block)
                        if size > max_file_bytes or read_bytes > max_total_bytes:
                            raise _IncompleteScan("read-limit")
                        content.update(block)
                    if size != expected[3] or _metadata(os.fstat(source.fileno())) != expected:
                        raise _IncompleteScan("race")
                    digest.update(content.digest())
        if before != inventory():
            raise _IncompleteScan("race")
        check_budget()
    except _IncompleteScan as exc:
        limitations.add(str(exc))
    except (OSError, ValueError, OverflowError, RecursionError):
        limitations.add("unreadable-or-identity")
    return WorkspaceSnapshot(scope, digest.hexdigest(), not limitations, tuple(sorted(limitations)))
