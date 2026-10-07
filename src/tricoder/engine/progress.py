"""单任务、有限窗口的失败与无进展检测纯逻辑。"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from enum import Enum


MAX_PROGRESS_OBSERVATIONS = 32


class ProgressAction(str, Enum):
    """一次观察之后由 Runner 采取的固定动作。"""

    CONTINUE = "continue"
    WARN = "warn"
    STOP = "stop"


class ProgressReason(str, Enum):
    """可审计且不包含工具输出的收敛原因。"""

    REPEATED_FAILURE = "repeated_failure"
    REPEATED_OBSERVATION = "repeated_observation"
    REPAIR_OSCILLATION = "repair_oscillation"


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    """宿主规范化后的单次工具观察。

    所有 ``*_fingerprint`` 字段只能承载不可逆摘要，不能放源码、命令输出或
    用户回答原文。工作区扫描不完整时必须把 ``workspace_complete`` 设为
    ``False``，此时该观察不会成为“状态相同”的证据。
    """

    tool_name: str
    arguments_fingerprint: str
    result_fingerprint: str
    failure_classification: str | None
    workspace_fingerprint: str | None
    workspace_complete: bool
    read_only: bool = False
    check_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.tool_name, "tool_name"),
            (self.arguments_fingerprint, "arguments_fingerprint"),
            (self.result_fingerprint, "result_fingerprint"),
        ):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError(f"进展观察 {label} 无效")
        for value, label in (
            (self.failure_classification, "failure_classification"),
            (self.workspace_fingerprint, "workspace_fingerprint"),
            (self.check_fingerprint, "check_fingerprint"),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 128
            ):
                raise ValueError(f"进展观察 {label} 无效")
        if type(self.workspace_complete) is not bool:
            raise ValueError("进展观察 workspace_complete 必须为布尔值")
        if type(self.read_only) is not bool:
            raise ValueError("进展观察 read_only 必须为布尔值")
        if self.workspace_complete and self.workspace_fingerprint is None:
            raise ValueError("完整工作区观察必须携带内容摘要")


@dataclass(frozen=True, slots=True)
class ProgressDecision:
    """进展守卫的固定输出；摘要标识不包含原始参数或结果。"""

    action: ProgressAction = ProgressAction.CONTINUE
    reason: ProgressReason | None = None
    count: int = 0
    limit: int = 0
    summary_id: str = ""

    def __post_init__(self) -> None:
        if self.action is ProgressAction.CONTINUE:
            if self.reason is not None or self.count or self.limit or self.summary_id:
                raise ValueError("继续决定不能携带停止元数据")
            return
        if (
            not isinstance(self.reason, ProgressReason)
            or type(self.count) is not int
            or type(self.limit) is not int
            or not 1 <= self.count <= self.limit
            or not isinstance(self.summary_id, str)
            or len(self.summary_id) != 16
        ):
            raise ValueError("进展决定元数据无效")


@dataclass(frozen=True, slots=True)
class _StoredObservation:
    observation: ProgressObservation
    read_epoch: int


class ProgressGuard:
    """在一次任务内保存最近观察；新任务必须创建新实例。"""

    def __init__(self, *, capacity: int = MAX_PROGRESS_OBSERVATIONS) -> None:
        if type(capacity) is not int or capacity != MAX_PROGRESS_OBSERVATIONS:
            raise ValueError("首版进展守卫容量固定为 32")
        self._observations: deque[_StoredObservation] = deque(maxlen=capacity)
        self._read_epoch = 0

    @property
    def observation_count(self) -> int:
        return len(self._observations)

    def note_user_answer(self) -> None:
        """只重启重复读取区间，不清除失败或振荡证据。"""

        self._read_epoch += 1

    def note_workspace_change(self) -> None:
        """真实工作区变化开启新读取区间；既有失败与振荡证据仍保留。"""

        self._read_epoch += 1

    def observe(self, observation: ProgressObservation) -> ProgressDecision:
        if not isinstance(observation, ProgressObservation):
            raise ValueError("进展观察类型无效")
        stored = _StoredObservation(observation, self._read_epoch)
        self._observations.append(stored)
        if not observation.workspace_complete:
            return ProgressDecision()

        if observation.failure_classification is not None:
            oscillation = self._oscillation_decision(observation)
            if oscillation is not None:
                return oscillation
            repeated_failure = self._repeated_failure_decision(observation)
            if repeated_failure is not None:
                return repeated_failure

        if observation.read_only and observation.failure_classification is None:
            repeated_read = self._repeated_read_decision(stored)
            if repeated_read is not None:
                return repeated_read
        return ProgressDecision()

    def _repeated_failure_decision(
        self,
        observation: ProgressObservation,
    ) -> ProgressDecision | None:
        signature = self._failure_signature(observation)
        count = sum(
            1
            for stored in self._observations
            if stored.observation.workspace_complete
            and self._failure_signature(stored.observation) == signature
        )
        if count == 2:
            return self._decision(
                ProgressAction.WARN,
                ProgressReason.REPEATED_FAILURE,
                count,
                3,
                signature,
            )
        if count >= 3:
            return self._decision(
                ProgressAction.STOP,
                ProgressReason.REPEATED_FAILURE,
                3,
                3,
                signature,
            )
        return None

    def _repeated_read_decision(
        self,
        current: _StoredObservation,
    ) -> ProgressDecision | None:
        signature = self._read_signature(current)
        count = sum(
            1
            for stored in self._observations
            if stored.observation.workspace_complete
            and stored.observation.read_only
            and stored.observation.failure_classification is None
            and self._read_signature(stored) == signature
        )
        if count == 2:
            return self._decision(
                ProgressAction.WARN,
                ProgressReason.REPEATED_OBSERVATION,
                count,
                4,
                signature,
            )
        if count >= 4:
            return self._decision(
                ProgressAction.STOP,
                ProgressReason.REPEATED_OBSERVATION,
                4,
                4,
                signature,
            )
        return None

    def _oscillation_decision(
        self,
        observation: ProgressObservation,
    ) -> ProgressDecision | None:
        if observation.check_fingerprint is None:
            return None
        states: list[str] = []
        for stored in self._observations:
            item = stored.observation
            if (
                not item.workspace_complete
                or item.failure_classification is None
                or item.check_fingerprint != observation.check_fingerprint
            ):
                continue
            assert item.workspace_fingerprint is not None
            if not states or states[-1] != item.workspace_fingerprint:
                states.append(item.workspace_fingerprint)
        if len(states) < 5:
            return None
        tail = states[-5:]
        if tail[0] == tail[2] == tail[4] and tail[1] == tail[3] and tail[0] != tail[1]:
            signature = (
                ProgressReason.REPAIR_OSCILLATION.value,
                observation.check_fingerprint,
                *tail,
            )
            return self._decision(
                ProgressAction.STOP,
                ProgressReason.REPAIR_OSCILLATION,
                5,
                5,
                signature,
            )
        return None

    @staticmethod
    def _failure_signature(observation: ProgressObservation) -> tuple[str, ...]:
        return (
            observation.tool_name,
            observation.arguments_fingerprint,
            observation.failure_classification or "",
            observation.result_fingerprint,
            observation.workspace_fingerprint or "",
        )

    @staticmethod
    def _read_signature(stored: _StoredObservation) -> tuple[str, ...]:
        observation = stored.observation
        return (
            observation.tool_name,
            observation.arguments_fingerprint,
            observation.result_fingerprint,
            observation.workspace_fingerprint or "",
            str(stored.read_epoch),
        )

    @staticmethod
    def _decision(
        action: ProgressAction,
        reason: ProgressReason,
        count: int,
        limit: int,
        signature: tuple[str, ...],
    ) -> ProgressDecision:
        payload = json.dumps(signature, ensure_ascii=True, separators=(",", ":"))
        summary_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return ProgressDecision(action, reason, count, limit, summary_id)


__all__ = [
    "MAX_PROGRESS_OBSERVATIONS",
    "ProgressAction",
    "ProgressDecision",
    "ProgressGuard",
    "ProgressObservation",
    "ProgressReason",
]
