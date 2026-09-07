"""线程安全、单向传播的协作式取消令牌。"""

from __future__ import annotations

import threading
import time


class CancellationError(RuntimeError):
    """任务因显式取消而停止。"""


class CancellationToken:
    """可由 UI 线程触发，并从父任务单向传播到子任务。"""

    def __init__(self, parent: CancellationToken | None = None) -> None:
        self._parent = parent
        self._event = threading.Event()
        self._cancel_lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        """返回本令牌或任意祖先令牌是否已取消。"""

        return self._event.is_set() or (
            self._parent is not None and self._parent.is_cancelled
        )

    def cancel(self) -> bool:
        """触发取消；仅第一次状态迁移返回 ``True``。"""

        with self._cancel_lock:
            if self._event.is_set():
                return False
            self._event.set()
            return True

    def create_child(self) -> CancellationToken:
        """创建只接受父级取消、不反向传播的子令牌。"""

        return CancellationToken(parent=self)

    def raise_if_cancelled(self) -> None:
        """若已取消，抛出调用方可以单独处理的异常。"""

        if self.is_cancelled:
            raise CancellationError("操作已取消")

    def wait(self, timeout: float | None = None) -> bool:
        """等待本令牌或父令牌取消，超时返回 ``False``。"""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout 必须是非负数或 None")
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_cancelled:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._event.wait(
                0.05 if remaining is None else min(0.05, remaining)
            )
        return True
