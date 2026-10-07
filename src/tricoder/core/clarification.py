"""需求澄清的宿主无关数据契约。

这里的回答只是一段用户提供的信息，不携带审批、权限或验证能力。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, TypeAlias

from tricoder.core.cancellation import CancellationToken


MAX_QUESTION_CHARS = 1_000
MAX_OPTION_CHARS = 120
MAX_OPTIONS = 4
MAX_ANSWER_CHARS = 4_000


class ClarificationStatus(str, Enum):
    """一次澄清等待的唯一终态。"""

    ANSWERED = "answered"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


_UNAVAILABLE_REASONS = frozenset(
    {
        "noninteractive",
        "host_unavailable",
        "host_failed",
        "invalid_host_result",
        "workspace_changed",
        "workspace_scan_failed",
        "question_limit",
        "ui_closed",
    }
)


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    """由宿主签发 ID 的有界问题；选项不会限制自由文本回答。"""

    request_id: str
    question: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or len(self.request_id) > 128
        ):
            raise ValueError("澄清请求 ID 无效")
        if (
            not isinstance(self.question, str)
            or not self.question.strip()
            or len(self.question) > MAX_QUESTION_CHARS
        ):
            raise ValueError("澄清问题必须为 1 到 1000 个字符")
        if not isinstance(self.options, tuple):
            raise ValueError("澄清选项必须是元组")
        if self.options and not 2 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError("澄清选项必须为 2 到 4 项")
        normalized: list[str] = []
        for option in self.options:
            if (
                not isinstance(option, str)
                or not option.strip()
                or len(option) > MAX_OPTION_CHARS
            ):
                raise ValueError("澄清选项必须为 1 到 120 个字符")
            normalized.append(option.strip())
        if len(set(normalized)) != len(normalized):
            raise ValueError("澄清选项不能重复")
        object.__setattr__(self, "question", self.question.strip())
        object.__setattr__(self, "options", tuple(normalized))


@dataclass(frozen=True, slots=True)
class ClarificationResult:
    """澄清等待结果；只有 answered 可以携带答案。"""

    status: ClarificationStatus
    answer: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ClarificationStatus):
            raise ValueError("澄清状态无效")
        if self.status is ClarificationStatus.ANSWERED:
            if (
                not isinstance(self.answer, str)
                or not self.answer.strip()
                or len(self.answer) > MAX_ANSWER_CHARS
                or self.reason is not None
            ):
                raise ValueError("回答必须为 1 到 4000 个字符且不能携带失败原因")
            return
        if self.answer is not None:
            raise ValueError("未回答状态不能携带答案")
        if self.status is ClarificationStatus.UNAVAILABLE:
            if self.reason not in _UNAVAILABLE_REASONS:
                raise ValueError("不可用原因无效")
        elif self.reason is not None:
            raise ValueError("只有 unavailable 可以携带原因")

    @classmethod
    def answered(cls, answer: str) -> "ClarificationResult":
        return cls(ClarificationStatus.ANSWERED, answer)

    @classmethod
    def cancelled(cls) -> "ClarificationResult":
        return cls(ClarificationStatus.CANCELLED)

    @classmethod
    def timed_out(cls) -> "ClarificationResult":
        return cls(ClarificationStatus.TIMED_OUT)

    @classmethod
    def unavailable(cls, reason: str) -> "ClarificationResult":
        return cls(ClarificationStatus.UNAVAILABLE, reason=reason)


ClarifierReturn: TypeAlias = ClarificationResult | Awaitable[ClarificationResult]
Clarifier: TypeAlias = Callable[
    [ClarificationRequest, CancellationToken, float], ClarifierReturn
]
