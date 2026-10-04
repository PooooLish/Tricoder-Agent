"""线程间传递一次性审批决定；关闭和迟到回调不能重新批准。"""

from __future__ import annotations

import threading
import time

from tricoder.core.cancellation import CancellationToken


class ApprovalWait:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._approved: bool | None = None

    def resolve(self, approved: bool) -> bool:
        # 终态与唤醒在同一锁内发布；任何迟到批准都不能覆盖拒绝。
        with self._lock:
            if self._approved is not None:
                return False
            self._approved = bool(approved)
            self._done.set()
            return True

    def close(self) -> bool:
        return self.resolve(False)

    def wait(self, cancellation: CancellationToken, timeout: float = 300.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if cancellation.is_cancelled:
                self.close()
                return False
            with self._lock:
                if self._approved is not None:
                    return self._approved
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                return False
            # 等待从不持有结果锁；50ms 是取消轮询上界，不是任务预算。
            self._done.wait(min(0.05, remaining))
