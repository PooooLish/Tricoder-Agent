"""Agent、工具和子任务共用的线程安全执行预算。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


class BudgetExceeded(RuntimeError):
    """某一项执行预算不足或已到达截止时间。"""

    def __init__(
        self,
        resource: str,
        *,
        requested: int | float,
        remaining: int | float,
    ) -> None:
        self.resource = resource
        self.requested = requested
        self.remaining = remaining
        super().__init__(f"{resource} 预算不足")


@dataclass(slots=True)
class ExecutionBudget:
    """按轮次、token、时间和子任务数限制一次执行。

    多维消费在同一把锁内先完整校验、再统一扣减，因此某一维拒绝时不会
    部分消耗其他维度，并发调用也不会让剩余值变成负数。
    """

    max_rounds: int
    max_tokens: int
    timeout_seconds: float
    max_subtasks: int
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _remaining_rounds: int = field(init=False, repr=False)
    _remaining_tokens: int = field(init=False, repr=False)
    _remaining_subtasks: int = field(init=False, repr=False)
    _started_at: float = field(init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        self._validate_count("max_rounds", self.max_rounds)
        self._validate_count("max_tokens", self.max_tokens)
        self._validate_count("max_subtasks", self.max_subtasks)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds 必须是非负数")
        if not callable(self.clock):
            raise ValueError("clock 必须可调用")

        self._remaining_rounds = self.max_rounds
        self._remaining_tokens = self.max_tokens
        self._remaining_subtasks = self.max_subtasks
        self._started_at = self.clock()

    @staticmethod
    def _validate_count(name: str, value: object) -> None:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} 必须是非负整数")

    @property
    def remaining_rounds(self) -> int:
        with self._lock:
            return self._remaining_rounds

    @property
    def remaining_tokens(self) -> int:
        with self._lock:
            return self._remaining_tokens

    @property
    def remaining_subtasks(self) -> int:
        with self._lock:
            return self._remaining_subtasks

    @property
    def remaining_seconds(self) -> float:
        with self._lock:
            return self._remaining_seconds_locked()

    def _remaining_seconds_locked(self) -> float:
        elapsed = self.clock() - self._started_at
        return max(0.0, float(self.timeout_seconds) - elapsed)

    def _raise_if_time_exceeded_locked(self) -> None:
        remaining = self._remaining_seconds_locked()
        if remaining <= 0:
            raise BudgetExceeded("time", requested=0, remaining=0)

    def raise_if_exceeded(self) -> None:
        """检查时间硬上限；计数维度在消费时检查。"""

        with self._lock:
            self._raise_if_time_exceeded_locked()

    def consume(
        self,
        *,
        rounds: int = 0,
        tokens: int = 0,
        subtasks: int = 0,
    ) -> None:
        """原子消费一组预算，不足时不修改任何剩余值。"""

        requests = {
            "rounds": rounds,
            "tokens": tokens,
            "subtasks": subtasks,
        }
        for name, value in requests.items():
            self._validate_count(name, value)

        with self._lock:
            self._raise_if_time_exceeded_locked()
            remaining = {
                "rounds": self._remaining_rounds,
                "tokens": self._remaining_tokens,
                "subtasks": self._remaining_subtasks,
            }
            for resource, requested in requests.items():
                if requested > remaining[resource]:
                    raise BudgetExceeded(
                        resource,
                        requested=requested,
                        remaining=remaining[resource],
                    )

            self._remaining_rounds -= rounds
            self._remaining_tokens -= tokens
            self._remaining_subtasks -= subtasks
