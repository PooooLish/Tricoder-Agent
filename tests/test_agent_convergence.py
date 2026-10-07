"""真实工具批次接入 ProgressGuard 后的收敛与历史回归。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.context.memory import ConversationMemory, with_task_termination_facts
from tricoder.context.spill import ToolResultSpillStore
from tricoder.core.cancellation import (
    CancellationError,
    CancellationToken,
    NativeCancellationError,
)
from tricoder.core.clarification import ClarificationResult
from tricoder.core.events import ToolExecutionCompleted
from tricoder.models import ProviderResponse, SessionContext, ToolCall, ToolDefinition
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.protocols import NativeToolProtocol
from tricoder.session.runtime import RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore
from tricoder.task_cleanup import TaskCleanup
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.workspace.verification import VerificationScope


class QueueProvider:
    """严格消费本地响应；额外请求立即暴露为测试失败。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.histories: list[list[object]] = []

    def complete(
        self,
        messages: list[object],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        if not self.responses:
            raise AssertionError("Provider 响应队列已耗尽，Agent 发生了额外请求")
        return self.responses.pop(0)


def response(*calls: ToolCall) -> ProviderResponse:
    return ProviderResponse(tool_calls=tuple(calls), finish_reason="tool_calls")


def call(index: int, tool: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(f"call-{index}", tool, arguments)


class AgentConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 固定 LF，避免 Windows write_text 的首次 CRLF→工具原子写入 LF 被误当成 A。
        (self.root / "sample.py").write_bytes(b"VALUE = 0\n")

    def make_agent(
        self,
        responses: list[ProviderResponse],
        *,
        audit: AuditLogger | None = None,
        clarifier=None,  # type: ignore[no-untyped-def]
    ) -> tuple[CodingAgent, QueueProvider]:
        provider = QueueProvider(responses)
        tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.root),
                CommandPolicy(self.root),
                approver=lambda *_: True,
                clarifier=clarifier,
                timeout=5,
            )
        )
        return (
            CodingAgent(
                provider,
                tools,
                max_rounds=20,
                plan_enabled=False,
                audit=audit,
            ),
            provider,
        )

    def test_fourth_same_read_stops_and_skips_same_batch_write(self) -> None:
        read = {"path": "sample.py"}
        agent, provider = self.make_agent(
            [
                response(call(1, "read_file", read)),
                response(call(2, "read_file", read)),
                response(call(3, "read_file", read)),
                response(
                    call(4, "read_file", read),
                    call(
                        5,
                        "create_file",
                        {"path": "must-not-exist.txt", "content": "x"},
                    ),
                ),
            ]
        )

        turn = agent.run_with_context("不要重复读取", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(4, len(provider.histories))
        self.assertEqual(4, turn.result.tool_calls)
        self.assertFalse((self.root / "must-not-exist.txt").exists())
        self.assertEqual("task_termination", turn.context.messages[-1].kind)
        last_tool_results = [
            message
            for message in turn.context.messages
            if message.role == "tool" and message.tool_call_id in {"call-4", "call-5"}
        ]
        self.assertEqual(2, len(last_tool_results))
        skipped = json.loads(last_tool_results[1].content)["tool_result"]
        self.assertEqual("skipped", skipped["error"]["code"])

    def test_equivalent_read_paths_share_repeat_budget(self) -> None:
        agent, provider = self.make_agent(
            [
                response(call(1, "read_file", {"path": "sample.py"})),
                response(call(2, "read_file", {"path": "./sample.py"})),
                response(call(3, "read_file", {"path": "sample.py"})),
                response(call(4, "read_file", {"path": ".\\sample.py"})),
            ]
        )

        turn = agent.run_with_context("规范化重复读取", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(4, len(provider.histories))

    def test_alternating_read_tools_do_not_reset_an_existing_read_budget(self) -> None:
        agent, provider = self.make_agent(
            [
                response(call(1, "read_file", {"path": "sample.py"})),
                response(call(2, "list_files", {"path": "."})),
                response(call(3, "read_file", {"path": "sample.py"})),
                response(call(4, "list_files", {"path": "."})),
                response(call(5, "read_file", {"path": "sample.py"})),
                response(call(6, "list_files", {"path": "."})),
                response(call(7, "read_file", {"path": "sample.py"})),
            ]
        )

        turn = agent.run_with_context("交替读取也要有界", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(7, len(provider.histories))

    def test_fourth_information_command_stops_as_repeated_observation(self) -> None:
        command = {"command": "python --version"}
        agent, provider = self.make_agent(
            [
                response(call(index, "run_command", command))
                for index in range(1, 5)
            ]
        )

        turn = agent.run_with_context("不要重复查询版本", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(4, len(provider.histories))

    def test_clarification_reprompt_requires_approval_then_can_verify_and_finish(
        self,
    ) -> None:
        approvals: list[tuple[str, str]] = []

        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            return ClarificationResult.answered("创建 chosen.py")

        provider = QueueProvider(
            [
                response(call(1, "ask_user", {"question": "创建哪个文件？"})),
                response(
                    call(
                        2,
                        "create_file",
                        {"path": "chosen.py", "content": "VALUE = 1\n"},
                    )
                ),
                response(
                    call(
                        3,
                        "run_command",
                        {"command": "python -m compileall -q chosen.py"},
                    )
                ),
                response(call(4, "finish", {"summary": "已按回答创建并验证"})),
            ]
        )
        tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.root),
                CommandPolicy(self.root),
                approver=lambda action, detail: approvals.append((action, detail))
                or True,
                clarifier=clarify,
                timeout=5,
            )
        )
        agent = CodingAgent(provider, tools, max_rounds=10, plan_enabled=False)

        turn = agent.run_with_context("先问清文件名再创建", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual(4, len(provider.histories))
        self.assertEqual("VALUE = 1\n", (self.root / "chosen.py").read_text("utf-8"))
        self.assertEqual(2, len(approvals))
        self.assertEqual(["create_file", "run_command"], [item[0] for item in approvals])

    def test_failure_read_failure_read_failure_stops_on_third_failure(self) -> None:
        (self.root / "test_failure.py").write_text(
            "import unittest\n\n"
            "class Failure(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        command = {"command": "python -m unittest -v test_failure"}
        agent, provider = self.make_agent(
            [
                response(call(1, "run_command", command)),
                response(call(2, "read_file", {"path": "sample.py"})),
                response(call(3, "run_command", command)),
                response(call(4, "list_files", {"path": "."})),
                response(call(5, "run_command", command)),
                response(
                    call(
                        6,
                        "edit_file",
                        {
                            "path": "test_failure.py",
                            "old_text": "self.assertEqual(1, 2)",
                            "new_text": "self.assertEqual(2, 2)",
                        },
                    )
                ),
                response(call(7, "run_command", command)),
                response(call(8, "finish", {"summary": "已修复并验证"})),
            ]
        )

        failed = agent.run_with_context("不要重复同一失败", SessionContext())

        self.assertFalse(failed.result.ok)
        self.assertIn("重复失败", failed.result.summary)
        self.assertEqual(5, len(provider.histories))
        self.assertEqual(5, failed.result.tool_calls)
        self.assertEqual("task_termination", failed.context.messages[-1].kind)
        warning_messages = [
            message
            for message in provider.histories[3]
            if getattr(message, "kind", None) == "protocol_feedback"
        ]
        self.assertTrue(warning_messages)
        self.assertIn("2/3", warning_messages[-1].content)

        succeeded = agent.run_with_context("修复失败并验证", failed.context)

        self.assertTrue(succeeded.result.ok, succeeded.result.summary)
        self.assertEqual(8, len(provider.histories))
        candidate_plan = ContextManager(
            ContextBudget(max_chars=100_000), NativeToolProtocol()
        ).plan_save_candidate(succeeded.context.messages, covered_through=0)
        candidate = with_task_termination_facts(
            ConversationMemory(), candidate_plan.source_messages
        )
        self.assertEqual(1, len(candidate.open_items))
        self.assertIn("重复失败", candidate.open_items[0].text)

    def test_a_b_a_b_a_failed_check_stops_as_repair_oscillation(self) -> None:
        audit_path = self.root / "oscillation-audit.jsonl"
        (self.root / "test_value.py").write_text(
            "import unittest\n"
            "from sample import VALUE\n\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(2, VALUE)\n",
            encoding="utf-8",
        )
        command = {"command": "python -m unittest -v test_value"}
        responses: list[ProviderResponse] = []
        current = 0
        call_index = 1
        for next_value in (1, 0, 1, 0):
            responses.append(response(call(call_index, "run_command", command)))
            call_index += 1
            responses.append(
                response(
                    call(
                        call_index,
                        "edit_file",
                        {
                            "path": "sample.py",
                            "old_text": f"VALUE = {current}",
                            "new_text": f"VALUE = {next_value}",
                        },
                    )
                )
            )
            call_index += 1
            current = next_value
        responses.append(response(call(call_index, "run_command", command)))
        responses.append(
            response(
                call(
                    call_index + 1,
                    "edit_file",
                    {
                        "path": "sample.py",
                        "old_text": "VALUE = 0",
                        "new_text": "VALUE = 2",
                    },
                )
            )
        )
        responses.append(response(call(call_index + 2, "run_command", command)))
        responses.append(
            response(call(call_index + 3, "finish", {"summary": "已修复并验证"}))
        )
        agent, provider = self.make_agent(responses, audit=AuditLogger(audit_path))

        failed = agent.run_with_context("修复测试但不要来回改", SessionContext())

        progress_events = [
            json.loads(line)
            for line in audit_path.read_text("utf-8").splitlines()
            if json.loads(line).get("status") in {"progress_warning", "progress_stop"}
        ]
        self.assertFalse(failed.result.ok, progress_events)
        self.assertEqual(
            "repair_oscillation",
            progress_events[-1].get("reason") if progress_events else None,
            progress_events,
        )
        self.assertIn("来回抵消", failed.result.summary)
        self.assertEqual(9, len(provider.histories))
        self.assertEqual(9, failed.result.tool_calls)
        self.assertEqual("VALUE = 0\n", (self.root / "sample.py").read_text("utf-8"))
        self.assertEqual("失败", failed.result.verification)

        succeeded = agent.run_with_context("采用新方案修复", failed.context)

        self.assertTrue(succeeded.result.ok, succeeded.result.summary)
        self.assertEqual(12, len(provider.histories))
        plan = ContextManager(
            ContextBudget(max_chars=100_000), NativeToolProtocol()
        ).plan_save_candidate(succeeded.context.messages, covered_through=0)
        candidate = with_task_termination_facts(
            ConversationMemory(), plan.source_messages
        )
        self.assertEqual(1, len(candidate.open_items))
        self.assertIn("来回抵消", candidate.open_items[0].text)

    def test_stop_history_allows_next_task_and_memory_keeps_failure_fact(self) -> None:
        read = {"path": "sample.py"}
        agent, provider = self.make_agent(
            [
                response(call(1, "read_file", read)),
                response(call(2, "read_file", read)),
                response(call(3, "read_file", read)),
                response(call(4, "read_file", read)),
                response(call(5, "finish", {"summary": "后续任务完成"})),
            ]
        )

        failed = agent.run_with_context("旧失败任务", SessionContext())
        succeeded = agent.run_with_context("后续正常任务", failed.context)

        self.assertFalse(failed.result.ok)
        self.assertTrue(succeeded.result.ok, succeeded.result.summary)
        self.assertEqual(5, len(provider.histories))
        self.assertGreater(failed.context.latest_completed_task_seq, 0)
        self.assertGreater(succeeded.context.latest_completed_task_seq, 0)
        manager = ContextManager(ContextBudget(max_chars=100_000), NativeToolProtocol())
        plan = manager.plan_save_candidate(
            succeeded.context.messages,
            covered_through=0,
        )
        self.assertTrue(plan.needs_summary, plan.reason)
        candidate = with_task_termination_facts(
            ConversationMemory(),
            plan.source_messages,
        )
        self.assertEqual(1, len(candidate.open_items))
        self.assertIn("重复读取", candidate.open_items[0].text)

    def test_progress_audit_contains_only_fixed_metadata(self) -> None:
        audit_path = self.root / "audit.jsonl"
        audit = AuditLogger(audit_path)
        read = {"path": "sample.py"}
        agent, _provider = self.make_agent(
            [
                response(call(1, "read_file", read)),
                response(call(2, "read_file", read)),
                response(call(3, "finish", {"summary": "停止前结束"})),
            ],
            audit=audit,
        )

        agent.run("审计重复读取提醒")

        events = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
        progress = [event for event in events if event.get("status") == "progress_warning"]
        self.assertEqual(1, len(progress))
        self.assertEqual("repeated_observation", progress[0]["reason"])
        self.assertEqual(2, progress[0]["count"])
        self.assertEqual(4, progress[0]["limit"])
        self.assertEqual(16, len(progress[0]["summary_id"]))
        self.assertNotIn("arguments", progress[0])
        self.assertNotIn("output", progress[0])

    def test_progress_stop_audit_failure_has_priority_and_records_incomplete(self) -> None:
        class FailingProgressAudit:
            def prepare(self) -> None:
                return None

            def log(self, event):  # type: ignore[no-untyped-def]
                if event.get("status") == "progress_warning":
                    raise OSError("synthetic progress audit failure")

        read = {"path": "sample.py"}
        agent, provider = self.make_agent(
            [
                response(call(1, "read_file", read)),
                response(call(2, "read_file", read)),
            ],
            audit=FailingProgressAudit(),  # type: ignore[arg-type]
        )

        turn = agent.run_with_context("审计失败优先", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("无法写入审计日志，运行已安全停止", turn.result.summary)
        self.assertEqual(2, len(provider.histories))
        self.assertTrue(
            any(message.kind == "task_termination" for message in turn.context.messages)
        )

    def test_cancellation_during_progress_stop_has_priority(self) -> None:
        token = CancellationToken()

        class CancellingProgressAudit:
            def prepare(self) -> None:
                return None

            def log(self, event):  # type: ignore[no-untyped-def]
                if event.get("status") == "progress_stop":
                    token.cancel()

        read = {"path": "sample.py"}
        agent, provider = self.make_agent(
            [response(call(index, "read_file", read)) for index in range(1, 5)],
            audit=CancellingProgressAudit(),  # type: ignore[arg-type]
        )

        turn = agent.run_with_context(
            "取消优先",
            SessionContext(),
            cancellation=token,
        )

        self.assertTrue(token.is_cancelled)
        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(4, len(provider.histories))
        self.assertTrue(
            any(message.kind == "task_termination" for message in turn.context.messages)
        )

    def test_cancellation_during_progress_snapshot_uses_normal_finalization(self) -> None:
        agent, provider = self.make_agent(
            [response(call(1, "read_file", {"path": "sample.py"}))]
        )

        with patch.object(
            VerificationScope,
            "capture",
            side_effect=CancellationError("synthetic snapshot cancellation"),
        ):
            turn = agent.run_with_context("快照期间取消", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(1, len(provider.histories))
        tool_results = [
            message
            for message in turn.context.messages
            if message.role == "tool" and message.tool_call_id == "call-1"
        ]
        self.assertEqual(1, len(tool_results))

    def test_failed_batch_progress_cancel_fills_remaining_once(self) -> None:
        audit_path = self.root / "f1-audit.jsonl"
        completed_events: list[ToolExecutionCompleted] = []
        agent, provider = self.make_agent(
            [
                response(
                    call(1, "read_file", {"path": 1}),
                    call(2, "read_file", {"path": "sample.py"}),
                ),
                response(call(3, "finish", {"summary": "后续任务完成"})),
            ],
            audit=AuditLogger(audit_path),
        )

        with patch.object(
            VerificationScope,
            "capture",
            side_effect=CancellationError("synthetic progress cancellation"),
        ):
            turn = agent.run_with_context(
                "失败批次扫描时取消",
                SessionContext(),
                event_sink=lambda event: (
                    completed_events.append(event)
                    if isinstance(event, ToolExecutionCompleted)
                    else None
                ),
            )

        counts = Counter(
            message.tool_call_id
            for message in turn.context.messages
            if message.role == "tool"
        )
        task_start = next(
            index
            for index, message in enumerate(turn.context.messages)
            if message.kind == "task"
        )
        task_block = list(turn.context.messages[task_start:])

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(1, len(provider.histories))
        self.assertEqual({"call-1": 1, "call-2": 1}, dict(counts))
        self.assertEqual(
            {"call-1": 1, "call-2": 1},
            dict(Counter(event.call_id for event in completed_events)),
        )
        audit_events = [
            json.loads(line) for line in audit_path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(
            1,
            sum(event.get("status") == "skipped" for event in audit_events),
        )
        self.assertTrue(
            ContextManager(
                ContextBudget(max_chars=100_000), NativeToolProtocol()
            )._is_closed_task_block(task_block)
        )
        succeeded = agent.run_with_context("取消后的后续任务", turn.context)
        self.assertTrue(succeeded.result.ok, succeeded.result.summary)
        save_plan = ContextManager(
            ContextBudget(max_chars=100_000), NativeToolProtocol()
        ).plan_save_candidate(succeeded.context.messages, covered_through=0)
        self.assertTrue(save_plan.needs_summary, save_plan.reason)

    def test_progress_cancel_skipped_audit_failure_keeps_one_result_per_call(self) -> None:
        class FailingSkippedAudit:
            def prepare(self) -> None:
                return None

            def log(self, event):  # type: ignore[no-untyped-def]
                if event.get("status") == "skipped":
                    raise OSError("synthetic skipped audit failure")

        agent, provider = self.make_agent(
            [
                response(
                    call(1, "read_file", {"path": "sample.py"}),
                    call(
                        2,
                        "create_file",
                        {"path": "must-not-exist.txt", "content": "x"},
                    ),
                )
            ],
            audit=FailingSkippedAudit(),  # type: ignore[arg-type]
        )

        with patch.object(
            VerificationScope,
            "capture",
            side_effect=CancellationError("synthetic progress cancellation"),
        ):
            turn = agent.run_with_context("扫描取消且审计失败", SessionContext())

        counts = Counter(
            message.tool_call_id
            for message in turn.context.messages
            if message.role == "tool"
        )
        self.assertFalse(turn.result.ok)
        self.assertEqual("无法写入审计日志，运行已安全停止", turn.result.summary)
        self.assertEqual(1, len(provider.histories))
        self.assertEqual({"call-1": 1, "call-2": 1}, dict(counts))
        self.assertFalse((self.root / "must-not-exist.txt").exists())

    def test_native_progress_cancel_keeps_cleanup_failure_and_single_pairing(self) -> None:
        agent, provider = self.make_agent(
            [
                response(
                    call(1, "read_file", {"path": 1}),
                    call(2, "read_file", {"path": "sample.py"}),
                )
            ]
        )
        native = NativeCancellationError(
            asyncio.CancelledError("synthetic native cancellation"),
            cleanup_owner=TaskCleanup(),
        )

        with patch.object(VerificationScope, "capture", side_effect=native):
            turn = agent.run_with_context("原生取消保留清理失败", SessionContext())

        counts = Counter(
            message.tool_call_id
            for message in turn.context.messages
            if message.role == "tool"
        )
        self.assertFalse(turn.result.ok)
        self.assertTrue(turn.result.cleanup_failed)
        self.assertEqual(1, len(provider.histories))
        self.assertEqual({"call-1": 1, "call-2": 1}, dict(counts))

    def test_spilled_identical_reads_stop_on_fourth_result_content(self) -> None:
        spill_temp = tempfile.TemporaryDirectory()
        self.addCleanup(spill_temp.cleanup)
        (self.root / "large.txt").write_text("fixed text\n" * 1_000, encoding="utf-8")
        store = ToolResultSpillStore(Path(spill_temp.name), "test-session")
        provider = QueueProvider(
            [
                *[
                    response(call(index, "read_file", {"path": "large.txt"}))
                    for index in range(1, 4)
                ],
                response(
                    call(4, "read_file", {"path": "large.txt"}),
                    call(
                        5,
                        "create_file",
                        {"path": "must-not-exist.txt", "content": "x"},
                    ),
                ),
                response(call(6, "finish", {"summary": "不应执行"})),
            ]
        )
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.root),
                CommandPolicy(self.root),
                approver=lambda *_: True,
                timeout=5,
                max_output_chars=100,
                spill_store=store,
            )
        )
        agent = CodingAgent(provider, registry, max_rounds=8, plan_enabled=False)

        turn = agent.run_with_context("重复读取大型文件", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(4, len(provider.histories))
        self.assertEqual(4, turn.result.tool_calls)
        self.assertFalse((self.root / "must-not-exist.txt").exists())
        spilled_outputs = [
            json.loads(message.content)["tool_result"]
            for message in turn.context.messages
            if message.role == "tool" and message.tool_call_id in {
                "call-1",
                "call-2",
                "call-3",
                "call-4",
            }
        ]
        self.assertEqual(4, len(spilled_outputs))
        # Provider 可见包装含随机引用；即使四条展示文本不同，进展身份仍应相同。
        self.assertEqual(4, len({item["output"] for item in spilled_outputs}))
        skipped = [
            json.loads(message.content)["tool_result"]
            for message in turn.context.messages
            if message.role == "tool" and message.tool_call_id == "call-5"
        ]
        self.assertEqual(1, len(skipped))
        self.assertEqual("skipped", skipped[0]["error"]["code"])

    def test_real_a_b_a_change_starts_new_read_interval(self) -> None:
        responses = [
            response(call(index, "read_file", {"path": "sample.py"}))
            for index in range(1, 4)
        ]
        responses.extend(
            [
                response(
                    call(
                        4,
                        "edit_file",
                        {
                            "path": "sample.py",
                            "old_text": "VALUE = 0",
                            "new_text": "VALUE = 1",
                        },
                    )
                ),
                response(
                    call(
                        5,
                        "edit_file",
                        {
                            "path": "sample.py",
                            "old_text": "VALUE = 1",
                            "new_text": "VALUE = 0",
                        },
                    )
                ),
                response(call(6, "read_file", {"path": "sample.py"})),
                response(call(7, "finish", {"summary": "已检查修改"})),
            ]
        )
        agent, provider = self.make_agent(responses)

        turn = agent.run_with_context("修改后重新读取", SessionContext())

        self.assertEqual(7, len(provider.histories))
        self.assertNotIn("重复读取", turn.result.summary)
        self.assertEqual("VALUE = 0\n", (self.root / "sample.py").read_text("utf-8"))

    def test_noop_directory_operation_does_not_restart_read_interval(self) -> None:
        (self.root / "existing").mkdir()
        responses = [
            response(call(index, "read_file", {"path": "sample.py"}))
            for index in range(1, 4)
        ]
        responses.extend(
            [
                response(
                    call(
                        4,
                        "create_directory",
                        {"path": "existing", "exist_ok": True},
                    )
                ),
                response(call(5, "read_file", {"path": "sample.py"})),
                response(call(6, "finish", {"summary": "不应执行"})),
            ]
        )
        agent, provider = self.make_agent(responses)

        turn = agent.run_with_context("no-op 不能制造进展", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(5, len(provider.histories))

    def test_four_reads_after_real_change_still_stop_in_new_interval(self) -> None:
        responses = [
            response(call(index, "read_file", {"path": "sample.py"}))
            for index in range(1, 4)
        ]
        responses.append(
            response(
                call(
                    4,
                    "edit_file",
                    {
                        "path": "sample.py",
                        "old_text": "VALUE = 0",
                        "new_text": "VALUE = 1",
                    },
                )
            )
        )
        responses.extend(
            response(call(index, "read_file", {"path": "sample.py"}))
            for index in range(5, 9)
        )
        responses.append(response(call(9, "finish", {"summary": "不应执行"})))
        agent, provider = self.make_agent(responses)

        turn = agent.run_with_context("变更后仍限制重复读取", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("重复读取", turn.result.summary)
        self.assertEqual(8, len(provider.histories))
        self.assertEqual("VALUE = 1\n", (self.root / "sample.py").read_text("utf-8"))

    def test_runtime_progress_stop_releases_locks_and_next_task_has_fresh_guard(self) -> None:
        read = {"path": "sample.py"}
        audit_temp = tempfile.TemporaryDirectory()
        self.addCleanup(audit_temp.cleanup)
        provider = QueueProvider(
            [
                *[response(call(index, "read_file", read)) for index in range(1, 5)],
                response(call(5, "read_file", read)),
                response(call(6, "finish", {"summary": "后续任务完成"})),
            ]
        )
        runtime = SessionRuntime(
            SessionStore(Path(audit_temp.name) / "runtime.db"),
            self.root,
            options=RuntimeOptions(
                environ={"OPENAI_API_KEY": "synthetic-key"},
                audit_dir=Path(audit_temp.name),
                plan_enabled=False,
                max_rounds=10,
            ),
            provider_factory=lambda _config, _timeout: provider,
            workspace_confirmer=lambda _preview: True,
        )
        self.addCleanup(runtime.close)

        first = runtime.run_task("触发重复读取停止")
        second = runtime.run_task("下一任务正常结束")

        self.assertFalse(first.ok)
        self.assertTrue(second.ok, second.summary)
        self.assertEqual(4, first.tool_calls)
        self.assertEqual(2, second.tool_calls)
        self.assertEqual(6, len(provider.histories))


if __name__ == "__main__":
    unittest.main()
