"""Provider 与 Agent Runtime 共用的类型化事件。

事件仅携带 TriCoder 自身的不可变数据模型。自由文本、工具参数和工具
输出均从 ``repr`` 中隐藏，避免调试日志意外记录潜在敏感内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias

from tricoder.models import RunResult, TokenUsage, ToolCall, ToolResult


class SubAgentState(str, Enum):
    """子 Agent 对父运行时公开的有限状态。"""

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Provider 生成的一段可展示文本。"""

    text: str = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """Provider 生成的一段思考文本。"""

    text: str = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """Provider 开始生成一次工具调用。"""

    call_id: str
    name: str
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """Provider 已完整生成、可以交给 Agent 校验的工具调用。"""

    call: ToolCall = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class UsageReported:
    """Provider 报告的规范化 token 用量。"""

    usage: TokenUsage
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ProviderCompleted:
    """Provider 已结束当前响应流。"""

    finish_reason: str | None = None
    agent_id: str = "root"


ProviderEvent: TypeAlias = (
    TextDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolCallCompleted
    | UsageReported
    | ProviderCompleted
)


@dataclass(frozen=True, slots=True)
class RoundStarted:
    """Agent 开始新一轮 Provider/工具交互。"""

    round_number: int
    max_rounds: int
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class PlanningCompleted:
    """无副作用规划阶段已生成计划。"""

    plan: str = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    """某个工具调用正在等待宿主审批。"""

    call: ToolCall = field(repr=False)
    reason: str = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ToolExecutionStarted:
    """经策略校验后，工具即将进入执行阶段。"""

    call: ToolCall = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ToolExecutionCompleted:
    """工具执行结束并返回规范化结果。"""

    call_id: str
    result: ToolResult = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class ContextCompacted:
    """上下文在预算边界内完成了一次压缩。"""

    before_chars: int
    after_chars: int
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class SubAgentStatusChanged:
    """子 Agent 生命周期状态发生变化。"""

    child_id: str
    state: SubAgentState
    safe_message: str = field(default="", repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class RuntimeFailed:
    """Agent Runtime 以经过归类的安全错误终止。"""

    category: str
    safe_message: str = field(repr=False)
    agent_id: str = "root"


@dataclass(frozen=True, slots=True)
class RuntimeCompleted:
    """Agent Runtime 正常结束并产生最终结果。"""

    result: RunResult = field(repr=False)
    agent_id: str = "root"


AgentEvent: TypeAlias = (
    ProviderEvent
    | RoundStarted
    | PlanningCompleted
    | ApprovalRequested
    | ToolExecutionStarted
    | ToolExecutionCompleted
    | ContextCompacted
    | SubAgentStatusChanged
    | RuntimeFailed
    | RuntimeCompleted
)


class EventSink(Protocol):
    """接收类型化事件的同步、无返回值回调契约。"""

    def __call__(self, event: AgentEvent) -> None: ...
