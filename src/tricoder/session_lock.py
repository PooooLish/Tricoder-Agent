"""兼容入口；Session 独占锁已迁至 :mod:`tricoder.session.lock`。"""

from tricoder.session.lock import SessionLock, SessionLockBusyError

__all__ = ["SessionLock", "SessionLockBusyError"]
