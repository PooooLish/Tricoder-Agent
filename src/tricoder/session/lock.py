"""本地 Session 的跨进程独占锁实现；进程退出由操作系统回收。"""

from __future__ import annotations

import errno
import hashlib
import os
import weakref
from pathlib import Path


class SessionLockBusyError(RuntimeError):
    """同一个数据库内的 Session 已有所有者。"""


class SessionLock:
    """持有文件描述符期间独占一个 Session，不依赖 PID 或过期时间。

    锁文件保留且不能在解锁时删除，否则等待者可能锁住不同 inode。
    这是本机协作式锁，不阻止绕过 Runtime 直接修改数据库的程序。
    """

    def __init__(self, database: Path, session_id: str) -> None:
        database = database.resolve()
        identity = os.path.normcase(str(database)) + "\0" + session_id
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        directory = database.parent / "session-locks"
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(directory / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                # Windows 允许锁定 EOF 之后的字节，无须在竞争时写锁文件。
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise SessionLockBusyError("该会话已被其他终端占用") from exc
            raise
        except BaseException:
            os.close(descriptor)
            raise
        self._release = weakref.finalize(self, os.close, descriptor)

    def close(self) -> None:
        """幂等关闭描述符，释放操作系统锁；不删除锁文件。"""
        self._release()
