"""同一规范工作区的任务级跨进程互斥锁实现。"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path


CONTROL_DIRECTORY = Path("runtime") / "tricoder-control"
LOCK_FILENAME = "workspace.lock"
ACTIVITY_FILENAME = "active-task.json"
GUARD_DIRECTORY_NAME = "workspace-locks-v1"


class WorkspaceLockError(RuntimeError):
    """工作区锁无法安全建立。"""


class WorkspaceLockBusyError(WorkspaceLockError):
    """同一工作区已有活动任务。"""


class WorkspaceIdentityError(WorkspaceLockError):
    """工作区或控制路径的文件身份无法证明。"""


class WorkspaceRecoveryRequiredError(WorkspaceLockError):
    """上次任务可能仍有未确认资源，必须人工核对后恢复。"""


def _is_reparse(metadata: os.stat_result) -> bool:
    """统一识别符号链接及 Windows junction/reparse point。"""

    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (stat.S_IFMT(metadata.st_mode), metadata.st_dev, metadata.st_ino)


def _absolute_without_resolving(path: Path) -> Path:
    """生成绝对拼写，但不先跟随链接掩盖不可信路径组件。"""

    return Path(os.path.abspath(os.fspath(path)))


def _verify_existing_directory_chain(path: Path) -> None:
    """逐组件拒绝链接/reparse；不能只检查解析后的末级目录。"""

    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe /= part
        metadata = probe.lstat()
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceIdentityError("工作区路径包含链接、重解析点或非目录组件")


def _ensure_plain_directory(path: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        pass
    metadata = path.lstat()
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceIdentityError("工作区控制目录不是可信普通目录")


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _close_descriptors(*descriptors: int) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


@dataclass(slots=True)
class _NamespaceGuard:
    """Linux 抽象 socket 锁；不依赖可被重命名的文件系统名称。"""

    socket: object

    def close(self) -> None:
        close = getattr(self.socket, "close", None)
        if callable(close):
            close()

    @property
    def alive(self) -> bool:
        fileno = getattr(self.socket, "fileno", None)
        return callable(fileno) and fileno() >= 0


def _open_namespace_guard(workspace_key: str) -> _NamespaceGuard | None:
    """在 Linux 内核命名空间取得不可由路径替换拆分的协作锁。"""

    if not sys.platform.startswith("linux"):
        return None
    import socket

    digest = hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()
    namespace = f"\0tricoder-workspace-{digest}"
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.set_inheritable(False)
    try:
        endpoint.bind(namespace)
        endpoint.listen(1)
    except OSError as exc:
        endpoint.close()
        if exc.errno == errno.EADDRINUSE:
            raise WorkspaceLockBusyError("工作区正在执行其他任务") from exc
        raise WorkspaceLockError("无法安全取得工作区内核守卫锁") from exc
    return _NamespaceGuard(endpoint)


def _close_lock_resources(
    namespace_guard: _NamespaceGuard | None,
    *descriptors: int,
) -> None:
    _close_descriptors(*descriptors)
    if namespace_guard is not None:
        namespace_guard.close()


@dataclass(frozen=True, slots=True)
class _GuardBinding:
    """外部守卫的路径、打开描述符与取得锁时的文件身份。"""

    path: Path
    descriptor: int
    root_descriptor: int | None
    root_identity: tuple[int, int, int]
    lock_identity: tuple[int, int, int]


def _guard_root() -> Path:
    """返回不受 TMP/TEMP 影响的稳定用户级守卫目录。"""

    return _absolute_without_resolving(
        Path.home() / ".tricoder" / GUARD_DIRECTORY_NAME
    )


def _open_guard_lock(workspace_key: str) -> _GuardBinding:
    """在工作区外先锁稳定名称，防止内部控制目录替换产生第二把锁。"""

    guard_root = _guard_root()
    root_descriptor: int | None = None
    descriptor: int | None = None
    try:
        guard_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _verify_existing_directory_chain(guard_root)
        metadata = guard_root.lstat()
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceIdentityError("工作区锁守卫目录身份不可信")
        try:
            os.chmod(guard_root, 0o700)
        except OSError:
            # Windows ACL 不由 chmod 完整表达；后续仍以普通目录和文件身份约束。
            if os.name != "nt":
                raise
        if os.name != "nt":
            root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            root_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            root_descriptor = os.open(guard_root, root_flags)
            opened_root = os.fstat(root_descriptor)
            if (
                _is_reparse(opened_root)
                or not stat.S_ISDIR(opened_root.st_mode)
                or _identity(opened_root) != _identity(metadata)
            ):
                raise WorkspaceIdentityError("工作区锁守卫目录绑定失败")

        digest = hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()
        filename = f"{digest}.lock"
        path = guard_root / filename
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if root_descriptor is None:
            descriptor = os.open(path, flags, 0o600)
            named = path.lstat()
        else:
            descriptor = os.open(filename, flags, 0o600, dir_fd=root_descriptor)
            named = os.stat(filename, dir_fd=root_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (
            _is_reparse(opened)
            or _is_reparse(named)
            or not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(named)
        ):
            raise WorkspaceIdentityError("工作区外部守卫锁身份不可信")
        _lock_descriptor(descriptor)
        return _GuardBinding(
            path=path,
            descriptor=descriptor,
            root_descriptor=root_descriptor,
            root_identity=_identity(metadata),
            lock_identity=_identity(opened),
        )
    except BaseException:
        _close_descriptors(
            *(item for item in (root_descriptor, descriptor) if item is not None)
        )
        raise


@dataclass(slots=True, weakref_slot=True)
class WorkspaceLock:
    """持有描述符期间独占工作区；关闭时保留锁文件。"""

    workspace: Path
    lock_path: Path
    guard_path: Path
    _root_identity: tuple[int, int, int]
    _control_identity: tuple[int, int, int]
    _lock_identity: tuple[int, int, int]
    _guard_root_identity: tuple[int, int, int]
    _guard_identity: tuple[int, int, int]
    _guard_descriptor: int
    _guard_root_descriptor: int | None
    _internal_descriptor: int
    _namespace_guard: _NamespaceGuard | None
    _release: weakref.finalize
    _activity_identity: tuple[int, int, int] | None = None

    @classmethod
    def acquire(cls, root: Path) -> "WorkspaceLock":
        """非阻塞取得锁；失败时绝不降级为无锁执行。"""

        return cls._acquire(root, allow_stale_activity=False)

    @classmethod
    def _acquire(
        cls,
        root: Path,
        *,
        allow_stale_activity: bool,
    ) -> "WorkspaceLock":

        written_root = _absolute_without_resolving(Path(root))
        try:
            _verify_existing_directory_chain(written_root)
            root_before = written_root.lstat()
        except (OSError, ValueError, WorkspaceIdentityError) as exc:
            if isinstance(exc, WorkspaceIdentityError):
                raise
            raise WorkspaceIdentityError("无法证明工作区目录身份") from exc
        if _is_reparse(root_before) or not stat.S_ISDIR(root_before.st_mode):
            raise WorkspaceIdentityError("工作区必须是非链接普通目录")

        # resolve 只在逐组件拒绝链接之后用于统一大小写/短长路径等宿主别名。
        workspace = written_root.resolve(strict=True)
        workspace_key = os.path.normcase(os.path.normpath(str(workspace)))
        namespace_guard = _open_namespace_guard(workspace_key)
        guard: _GuardBinding | None = None
        descriptor: int | None = None
        try:
            guard = _open_guard_lock(workspace_key)
        except OSError as exc:
            if namespace_guard is not None:
                namespace_guard.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise WorkspaceLockBusyError("工作区正在执行其他任务") from exc
            raise WorkspaceLockError("无法安全取得工作区外部守卫锁") from exc
        except BaseException:
            if namespace_guard is not None:
                namespace_guard.close()
            raise
        control_parent = workspace / CONTROL_DIRECTORY.parent
        control = workspace / CONTROL_DIRECTORY
        try:
            _ensure_plain_directory(control_parent)
            _ensure_plain_directory(control)
            control_identity = _identity(control.lstat())
        except (OSError, ValueError, WorkspaceIdentityError) as exc:
            if guard is not None:
                _close_lock_resources(
                    namespace_guard,
                    *(item for item in (guard.root_descriptor, guard.descriptor) if item is not None)
                )
            if isinstance(exc, WorkspaceIdentityError):
                raise
            raise WorkspaceIdentityError("无法安全建立工作区控制目录") from exc

        lock_path = control / LOCK_FILENAME
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            opened = os.fstat(descriptor)
            named = lock_path.lstat()
            if (
                _is_reparse(opened)
                or _is_reparse(named)
                or not stat.S_ISREG(opened.st_mode)
                or _identity(opened) != _identity(named)
            ):
                raise WorkspaceIdentityError("工作区锁文件身份不可信")
            _lock_descriptor(descriptor)

            root_after = workspace.lstat()
            if _is_reparse(root_after) or _identity(root_after) != _identity(root_before):
                raise WorkspaceIdentityError("取得锁期间工作区目录身份发生变化")
            activity_path = control / ACTIVITY_FILENAME
            activity_identity: tuple[int, int, int] | None = None
            try:
                activity = activity_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if _is_reparse(activity) or not stat.S_ISREG(activity.st_mode):
                    raise WorkspaceIdentityError("工作区活动标记身份不可信")
                activity_identity = _identity(activity)
                if not allow_stale_activity:
                    raise WorkspaceRecoveryRequiredError(
                        "检测到上次任务的活动标记；请确认没有遗留进程后再恢复"
                    )
        except OSError as exc:
            _close_lock_resources(namespace_guard, *(
                item
                for item in (
                    descriptor,
                    guard.descriptor if guard is not None else None,
                    guard.root_descriptor if guard is not None else None,
                )
                if item is not None
            ))
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise WorkspaceLockBusyError("工作区正在执行其他任务") from exc
            raise WorkspaceLockError("无法安全取得工作区任务锁") from exc
        except BaseException:
            _close_lock_resources(namespace_guard, *(
                item
                for item in (
                    descriptor,
                    guard.descriptor if guard is not None else None,
                    guard.root_descriptor if guard is not None else None,
                )
                if item is not None
            ))
            raise

        assert guard is not None
        assert descriptor is not None

        instance = cls(
            workspace=workspace,
            lock_path=lock_path,
            guard_path=guard.path,
            _root_identity=_identity(root_before),
            _control_identity=control_identity,
            _lock_identity=_identity(opened),
            _guard_root_identity=guard.root_identity,
            _guard_identity=guard.lock_identity,
            _guard_descriptor=guard.descriptor,
            _guard_root_descriptor=guard.root_descriptor,
            _internal_descriptor=descriptor,
            _namespace_guard=namespace_guard,
            _release=None,  # type: ignore[arg-type]
            _activity_identity=activity_identity,
        )
        instance._release = weakref.finalize(
            instance,
            _close_lock_resources,
            namespace_guard,
            *(item for item in (guard.root_descriptor, guard.descriptor) if item is not None),
            descriptor,
        )
        return instance

    @property
    def activity_path(self) -> Path:
        return self.lock_path.parent / ACTIVITY_FILENAME

    def mark_active(self) -> None:
        """真正执行前建立最小标记；不写 Session、任务正文或 PID。"""

        self.ensure_workspace_stable()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.activity_path, flags, 0o600)
            payload = b'{"state":"active","version":1}\n'
            os.write(descriptor, payload)
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            named = self.activity_path.lstat()
            if (
                _is_reparse(opened)
                or _is_reparse(named)
                or not stat.S_ISREG(opened.st_mode)
                or _identity(opened) != _identity(named)
            ):
                raise WorkspaceIdentityError("工作区活动标记身份不可信")
            self._activity_identity = _identity(opened)
        except FileExistsError as exc:
            raise WorkspaceRecoveryRequiredError(
                "检测到未清理的工作区活动标记"
            ) from exc
        except OSError as exc:
            raise WorkspaceLockError("无法安全建立工作区活动标记") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def clear_active(self) -> None:
        """只删除本实例创建或显式接管的 exact 活动标记。"""

        expected = self._activity_identity
        if expected is None:
            return
        self.ensure_workspace_stable()
        try:
            current = self.activity_path.lstat()
        except FileNotFoundError as exc:
            raise WorkspaceIdentityError("工作区活动标记意外消失") from exc
        if _is_reparse(current) or _identity(current) != expected:
            raise WorkspaceIdentityError("工作区活动标记在清理前发生变化")
        try:
            self.activity_path.unlink()
        except OSError as exc:
            raise WorkspaceLockError("工作区活动标记清理失败") from exc
        self._activity_identity = None

    @classmethod
    def recover_stale_activity(cls, root: Path, *, confirmed: bool) -> bool:
        """确认外部资源已结束后，独占锁内清除遗留活动标记。"""

        if confirmed is not True:
            raise WorkspaceRecoveryRequiredError("必须明确确认旧任务资源已经清理")
        ownership = cls._acquire(root, allow_stale_activity=True)
        try:
            if ownership._activity_identity is None:
                return False
            ownership.clear_active()
            return True
        finally:
            ownership.close()

    def ensure_workspace_stable(self) -> None:
        """在关键边界复核工作区仍是取得锁时的同一目录对象。"""

        try:
            metadata = self.workspace.lstat()
            guard_root = self.guard_path.parent.lstat()
            guard_file = self.guard_path.lstat()
            opened_guard = os.fstat(self._guard_descriptor)
            opened_internal = os.fstat(self._internal_descriptor)
            opened_guard_root = (
                os.fstat(self._guard_root_descriptor)
                if self._guard_root_descriptor is not None
                else None
            )
        except OSError as exc:
            raise WorkspaceIdentityError("工作区或锁守卫已不可访问") from exc
        if _is_reparse(metadata) or _identity(metadata) != self._root_identity:
            raise WorkspaceIdentityError("工作区目录身份已变化")
        if (
            _is_reparse(guard_root)
            or _identity(guard_root) != self._guard_root_identity
            or _is_reparse(guard_file)
            or _identity(guard_file) != self._guard_identity
            or _identity(opened_guard) != self._guard_identity
            or (
                opened_guard_root is not None
                and _identity(opened_guard_root) != self._guard_root_identity
            )
        ):
            raise WorkspaceIdentityError("工作区锁守卫目录或文件身份已变化")
        if self._namespace_guard is not None and not self._namespace_guard.alive:
            raise WorkspaceIdentityError("工作区内核守卫锁已意外关闭")
        try:
            control = (self.workspace / CONTROL_DIRECTORY).lstat()
            lock_file = self.lock_path.lstat()
        except OSError as exc:
            raise WorkspaceIdentityError("工作区控制目录已不可访问") from exc
        if (
            _is_reparse(control)
            or _identity(control) != self._control_identity
            or _is_reparse(lock_file)
            or _identity(lock_file) != self._lock_identity
            or _identity(opened_internal) != self._lock_identity
        ):
            raise WorkspaceIdentityError("工作区控制目录或锁文件身份已变化")

    @property
    def closed(self) -> bool:
        return not self._release.alive

    def close(self) -> None:
        """幂等释放操作系统锁；锁文件必须保留。"""

        self._release()
