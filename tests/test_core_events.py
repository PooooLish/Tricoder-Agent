import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


# 让 ``python -m unittest`` 在未安装包的源码工作树中也能直接发现 ``src``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.core.events import (
    AgentEvent,
    ApprovalRequested,
    ContextCompacted,
    EventSink,
    PlanningCompleted,
    ProviderCompleted,
    ProviderEvent,
    RuntimeCompleted,
    RuntimeFailed,
    SubAgentState,
    SubAgentStatusChanged,
    TextDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    UsageReported,
)
from tricoder.models import RunResult, TokenUsage, ToolCall, ToolResult


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)


class CoreEventTests(unittest.TestCase):
    def test_provider_events_are_immutable_provider_independent_data(self) -> None:
        """防止流事件被原地篡改，或把厂商 SDK 原始对象带入核心层。"""
        call = ToolCall("call-1", "read_file", {"path": "README.md"})
        events: tuple[ProviderEvent, ...] = (
            TextDelta("hello"),
            ThinkingDelta("inspect"),
            ToolCallStarted("call-1", "read_file"),
            ToolCallCompleted(call),
            UsageReported(TokenUsage(input_tokens=10, output_tokens=2)),
            ProviderCompleted("stop"),
        )

        for event in events:
            with self.subTest(event=type(event).__name__):
                self.assertTrue(type(event).__module__.startswith("tricoder."))
                with self.assertRaises(FrozenInstanceError):
                    event.agent_id = "changed"  # type: ignore[misc]

    def test_agent_events_cover_runtime_lifecycle_as_pure_data(self) -> None:
        """防止规划、审批、工具、压缩、子任务与终态缺少统一事件表示。"""
        call = ToolCall("call-1", "read_file", {"path": "README.md"})
        result = ToolResult(True, "read ok")
        runtime_result = RunResult(True, "done", rounds=1)
        events: tuple[AgentEvent, ...] = (
            PlanningCompleted("1. inspect"),
            ApprovalRequested(call, "read source"),
            ToolExecutionStarted(call),
            ToolExecutionCompleted("call-1", result),
            ContextCompacted(before_chars=100, after_chars=40),
            SubAgentStatusChanged("child-1", SubAgentState.RUNNING, "working"),
            RuntimeFailed("provider", "temporarily unavailable"),
            RuntimeCompleted(runtime_result),
        )
        sink: EventSink = _CollectingSink()

        for event in events:
            sink(event)

        self.assertEqual(events, tuple(sink.events))
        self.assertTrue(all(type(event).__module__.startswith("tricoder.") for event in events))
        for event in events:
            with self.subTest(immutable=type(event).__name__):
                with self.assertRaises(FrozenInstanceError):
                    event.agent_id = "changed"  # type: ignore[misc]

    def test_event_repr_does_not_expose_dynamic_or_secret_bearing_content(self) -> None:
        """防止日志调试时通过事件 repr 泄露正文、参数或工具输出。"""
        secret = "sk-secret-value"
        call = ToolCall("call-1", "write_file", {"content": secret})
        events: tuple[AgentEvent, ...] = (
            TextDelta(secret),
            ThinkingDelta(secret),
            PlanningCompleted(secret),
            ApprovalRequested(call, secret),
            ToolCallCompleted(call),
            ToolExecutionStarted(call),
            ToolExecutionCompleted("call-1", ToolResult(True, secret)),
            SubAgentStatusChanged("child-1", SubAgentState.FAILED, secret),
            RuntimeFailed("provider", secret),
            RuntimeCompleted(RunResult(True, secret, rounds=1)),
        )

        for event in events:
            with self.subTest(event=type(event).__name__):
                self.assertNotIn(secret, repr(event))


if __name__ == "__main__":
    unittest.main()
