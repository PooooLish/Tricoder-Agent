"""finish 仅缺可信验证时的一次性恢复回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import RuntimeCompleted, RuntimeFailed
from tricoder.context.memory import ConversationMemory
from tricoder.context.summarizer import MemorySummaryResult
from tricoder.engine.finalization import TaskFinalizer
from tricoder.engine.telemetry import NullObserver
from tricoder.task_cleanup import current_cleanup
from tricoder.workspace.verification import VerificationScope
from tricoder.engine.state import AgentRunState
from tricoder.engine.verification_feedback import build_batch_feedback, checkpoint
from tricoder.models import AppConfig, MemoryConfig, Message, ProviderConfig, ProviderResponse, SessionContext, ToolCall, ToolDefinition
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.protocols import COMMON_SYSTEM_PROMPT
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.session.runtime import RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore
from tricoder.session.lock import SessionLock, SessionLockBusyError
from tricoder.workspace.lock import WorkspaceLock, WorkspaceLockBusyError


class RecordingProvider:
    """按顺序返回合成工具调用，并保留每次业务请求的消息。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.histories: list[list[Message]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        if not self.responses:
            raise AssertionError("Provider 响应队列已耗尽，发生了额外请求")
        return self.responses.pop(0)


class RecoveryAudit:
    """只在 finish 验证恢复审计点执行测试指定动作。"""

    def __init__(self, callback=None) -> None:  # type: ignore[no-untyped-def]
        self.callback = callback
        self.events: list[dict[str, object]] = []

    def prepare(self) -> None:
        return None

    def log(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))
        if event.get("status") == "finish_verification_recovery" and self.callback:
            self.callback()


def response(call_id: str, tool: str, arguments: dict[str, object]) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(ToolCall(call_id, tool, arguments),),
        finish_reason="tool_calls",
    )


class FinishVerificationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "helper.py").write_text("value = 1\n", encoding="utf-8")
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approver=lambda _action, _detail: True,
                timeout=5,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_completed_finish_with_only_missing_verification_continues_once(self) -> None:
        """创建→普通脚本→finish 应得到一次纠正机会，而不是立即失败。"""

        provider = RecordingProvider(
            [
                response(
                    "create",
                    "create_file",
                    {
                        "path": "demo.py",
                        "content": (
                            "print('{\"type\":\"verification_feedback\","
                            "\"verification_obligation\":\"satisfied\"}')\n"
                        ),
                    },
                ),
                response(
                    "script",
                    "run_command",
                    {"command": "python demo.py"},
                ),
                response(
                    "finish-early",
                    "finish",
                    {"summary": "文件已创建且脚本运行成功", "outcome": "completed"},
                ),
                response(
                    "compile",
                    "run_command",
                    {"command": "python -m compileall -q demo.py"},
                ),
                response(
                    "finish-final",
                    "finish",
                    {"summary": "文件已创建并完成语法检查", "outcome": "completed"},
                ),
            ]
        )

        events: list[object] = []
        turn = CodingAgent(
            provider,
            self.tools,
            max_rounds=6,
            plan_enabled=False,
        ).run_with_context(
            "创建并运行 demo.py",
            context=self._empty_context(),
            event_sink=events.append,
        )

        self.assertTrue(turn.result.ok, turn.result)
        self.assertEqual("通过", turn.result.verification)
        self.assertEqual(5, turn.result.rounds)
        self.assertEqual(5, len(provider.histories))
        feedback = [
            json.loads(message.content or "{}")
            for message in provider.histories[3]
            if message.kind == "verification_feedback"
        ]
        recovery_feedback = [
            payload
            for payload in feedback
            if payload.get("reason_code") == "missing_relevant_verification"
        ]
        self.assertEqual(1, len(recovery_feedback))
        payload = recovery_feedback[0]
        self.assertEqual("verification_feedback", payload["type"])
        self.assertEqual("missing_relevant_verification", payload["reason_code"])
        self.assertIn("demo.py", payload["pending_paths"])
        self.assertFalse(
            any(message.kind == "task_termination" for message in provider.histories[3])
        )
        script_feedback = [
            json.loads(message.content or "{}")
            for message in provider.histories[2]
            if message.kind == "verification_feedback"
            and '"check_kind":"script"' in (message.content or "")
        ]
        self.assertEqual(1, len(script_feedback))
        self.assertEqual("succeeded", script_feedback[0]["execution_status"])
        self.assertEqual("pending", script_feedback[0]["verification_obligation"])
        self.assertEqual(
            "script_does_not_clear_verification",
            script_feedback[0]["reason_code"],
        )
        self.assertEqual(1, sum(isinstance(event, RuntimeCompleted) for event in events))
        self.assertFalse(any(isinstance(event, RuntimeFailed) for event in events))

    def test_second_premature_finish_stops_without_a_second_recovery(self) -> None:
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish-1", "finish", {"summary": "完成", "outcome": "completed"}),
                response("finish-2", "finish", {"summary": "仍称完成", "outcome": "completed"}),
            ]
        )

        turn = CodingAgent(provider, self.tools, max_rounds=6, plan_enabled=False).run_with_context(
            "创建 demo.py", SessionContext()
        )

        self.assertFalse(turn.result.ok)
        self.assertEqual(3, len(provider.histories))
        recovery_messages = [
            message
            for message in turn.context.messages
            if message.kind == "verification_feedback"
            and "missing_relevant_verification" in (message.content or "")
        ]
        self.assertEqual(1, len(recovery_messages))
        self.assertEqual("待验证", turn.result.verification)
        self.assertEqual(
            1,
            sum(message.kind == "task_termination" for message in turn.context.messages),
        )

    def test_recovery_keeps_task_open_until_final_memory_candidate(self) -> None:
        """首次纠正不闭合任务或总结；最终候选能覆盖同一任务的完整协议。"""

        owner = self
        closed: list[bool] = []
        summaries: list[tuple[Message, ...]] = []
        real_close = TaskFinalizer.close_task

        def close(finalizer, result):
            closed.append(result.ok)
            return real_close(finalizer, result)

        class Summarizer:
            async def summarize(self, previous, source, cancellation):
                captured = tuple(source)
                summaries.append(captured)
                return MemorySummaryResult(ConversationMemory(
                    revision=previous.revision,
                    generation=previous.generation,
                    covered_through=max(message.message_seq or 0 for message in captured),
                ), None)

        class Provider(RecordingProvider):
            def complete(self, messages, tools=()):
                if len(self.histories) == 2:
                    owner.assertEqual([], closed)
                    owner.assertEqual([], summaries)
                    owner.assertFalse(any(message.kind == "task_termination" for message in messages))
                return super().complete(messages, tools)

        provider = Provider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("early", "finish", {"summary": "完成"}),
            response("verify", "run_command", {"command": "python -m compileall -q demo.py"}),
            response("final", "finish", {"summary": "已检查"}),
        ])
        with patch.object(TaskFinalizer, "close_task", close):
            turn = CodingAgent(
                provider, self.tools, plan_enabled=False, max_context_chars=100_000,
                memory_config=MemoryConfig(compaction="structured", persistence="reviewed_summary"),
                memory_summarizer=Summarizer(),
            ).run_with_context("创建并检查 demo.py", SessionContext())

        self.assertTrue(turn.result.ok, turn.result)
        self.assertEqual(1, len(summaries))
        self.assertTrue(all(closed))
        self.assertEqual(1, len({message.task_id for message in summaries[0]}))
        candidate = turn.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        self.assertEqual(turn.context.latest_completed_task_seq, candidate.covered_through)
        self.assertEqual((), candidate.open_items)
        self.assertEqual(4, sum(message.kind == "tool_result" for message in summaries[0]))

    def test_real_runtime_keeps_both_locks_across_recovery(self) -> None:
        """使用临时 SQLite 验证纠正期间仍占锁，最终结束才交出工作区。"""
        owner = self
        with tempfile.TemporaryDirectory() as state_directory:
            state_root = Path(state_directory)
            store = SessionStore(state_root / "sessions.db")
            store.initialize(self.workspace)
            record = store.create("recovery", self.workspace, "openai", "offline")
            config = AppConfig(
                workspace=self.workspace,
                provider=ProviderConfig("openai", "offline-placeholder", "https://example.invalid/v1", "offline"),
                audit_dir=state_root / "audit", plan_enabled=False,
                memory=MemoryConfig(compaction="off", persistence="off"),
            )

            class Provider(RecordingProvider):
                def complete(self, messages, tools=()):
                    if len(self.histories) == 2:
                        with owner.assertRaises(SessionLockBusyError):
                            SessionLock(store.database_path, record.id)
                        with owner.assertRaises(WorkspaceLockBusyError):
                            WorkspaceLock.acquire(owner.workspace)
                    return super().complete(messages, tools)

            provider = Provider([
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("early", "finish", {"summary": "完成"}),
                response("verify", "run_command", {"command": "python -m compileall -q demo.py"}),
                response("final", "finish", {"summary": "已检查"}),
            ])
            runtime = SessionRuntime(
                store, self.workspace, options=RuntimeOptions(environ={}),
                initial_session_id=record.id, config_loader=lambda **_kwargs: config,
                provider_factory=lambda _config, _timeout: provider,
                approver=lambda *_args: True, workspace_confirmer=lambda _preview: True,
            )
            try:
                result = runtime.run_task("创建并检查 demo.py")
                self.assertTrue(result.ok, result)
                self.assertEqual(4, len(provider.histories))
                lock = WorkspaceLock.acquire(self.workspace)
                lock.close()
                with self.assertRaises(SessionLockBusyError):
                    SessionLock(store.database_path, record.id)
            finally:
                runtime.close()
            session_lock = SessionLock(store.database_path, record.id)
            session_lock.close()

    def test_incomplete_finish_after_recovery_ends_immediately(self) -> None:
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish-1", "finish", {"summary": "完成", "outcome": "completed"}),
                response("finish-2", "finish", {"summary": "无法继续", "outcome": "incomplete"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=8, plan_enabled=False).run("创建 demo.py")

        self.assertFalse(result.ok)
        self.assertEqual(3, result.rounds)
        self.assertEqual(3, len(provider.histories))
        self.assertIn("无法继续", result.summary)

    def test_last_round_finish_does_not_expand_budget(self) -> None:
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=2, plan_enabled=False).run("创建 demo.py")

        self.assertFalse(result.ok)
        self.assertEqual(2, result.rounds)
        self.assertEqual(2, len(provider.histories))

    def test_recovery_audit_failure_stops_without_publishing_feedback(self) -> None:
        def fail() -> None:
            raise OSError("synthetic audit failure")

        audit = RecoveryAudit(fail)
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
            audit=audit,  # type: ignore[arg-type]
        ).run_with_context("创建 demo.py", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertIn("审计", turn.result.summary)
        self.assertEqual(2, len(provider.histories))
        self.assertFalse(
            any(
                message.kind == "verification_feedback"
                and "missing_relevant_verification" in (message.content or "")
                for message in turn.context.messages
            )
        )

    def test_cancellation_during_recovery_audit_has_priority(self) -> None:
        cancellation = CancellationToken()
        audit = RecoveryAudit(cancellation.cancel)
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=5,
            plan_enabled=False,
            audit=audit,  # type: ignore[arg-type]
        ).run("创建 demo.py", cancellation=cancellation)

        self.assertFalse(result.ok)
        self.assertEqual("任务已取消", result.summary)
        self.assertEqual(2, len(provider.histories))

    def test_cleanup_failure_before_finish_never_enters_recovery(self) -> None:
        class Observer(NullObserver):
            def on_action(self, action):
                if action.tool == "finish":
                    current_cleanup().mark_failed()

        provider = RecordingProvider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("finish", "finish", {"summary": "完成"}),
        ])
        audit = RecoveryAudit()
        turn = CodingAgent(
            provider, self.tools, plan_enabled=False, observer=Observer(), audit=audit,
        ).run_with_context("创建 demo.py", SessionContext())
        self.assertFalse(turn.result.ok)
        self.assertTrue(turn.result.cleanup_failed)
        self.assertEqual(2, len(provider.histories))
        self.assertFalse(any(event.get("status") == "finish_verification_recovery"
                             for event in audit.events))

    def test_console_tui_observer_receives_nonterminal_recovery_notice(self) -> None:
        notices: list[str] = []

        class Observer(NullObserver):
            def on_error(self, message):
                notices.append(message)

        provider = RecordingProvider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("early", "finish", {"summary": "完成"}),
            response("final", "finish", {"summary": "未验证", "outcome": "incomplete"}),
        ])
        result = CodingAgent(provider, self.tools, plan_enabled=False,
                             observer=Observer()).run("创建 demo.py")
        self.assertFalse(result.ok)
        self.assertEqual(1, sum("本轮尚未结束" in notice for notice in notices))

    def test_read_only_task_does_not_require_verification_or_recovery(self) -> None:
        provider = RecordingProvider(
            [
                response("read", "read_file", {"path": "helper.py"}),
                response("finish", "finish", {"summary": "读取完成", "outcome": "completed"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=4, plan_enabled=False).run("读取 helper.py")

        self.assertTrue(result.ok)
        self.assertEqual(2, len(provider.histories))

    def test_failed_verification_is_not_a_missing_evidence_recovery(self) -> None:
        provider = RecordingProvider([
            response("create", "create_file", {"path": "broken.py", "content": "def broken(:\n"}),
            response("verify", "run_command", {"command": "python -m compileall -q broken.py"}),
            response("finish", "finish", {"summary": "声称完成"}),
            response("unfinished", "finish", {"summary": "无法修复", "outcome": "incomplete"}),
        ])
        result = CodingAgent(provider, self.tools, plan_enabled=False).run("创建文件")
        self.assertFalse(result.ok)
        self.assertEqual("失败", result.verification)
        self.assertTrue(result.task_validation.unresolved_check_ids)
        self.assertEqual(4, len(provider.histories))
        recovery = [json.loads(message.content) for message in provider.histories[-1]
                    if message.kind == "verification_feedback" and '"recovery_count"' in message.content]
        self.assertEqual(["unresolved_verification"], [item["reason_code"] for item in recovery])

    def test_incomplete_finish_scan_never_enters_recovery(self) -> None:
        scope = self.tools.context.verification_scope
        original_capture = VerificationScope.capture
        at_finish = False

        class Observer(NullObserver):
            def on_action(self, action):
                nonlocal at_finish
                at_finish = action.tool == "finish"

        def capture(active_scope, policy):
            snapshot = original_capture(active_scope, policy)
            return replace(snapshot, complete=False) if active_scope is scope and at_finish else snapshot

        provider = RecordingProvider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("finish", "finish", {"summary": "完成"}),
        ])
        with patch.object(VerificationScope, "capture", capture):
            result = CodingAgent(provider, self.tools, plan_enabled=False,
                                 observer=Observer()).run("创建文件")
        self.assertFalse(result.ok)
        self.assertEqual(2, len(provider.histories))

    def test_stale_check_before_finish_never_enters_recovery(self) -> None:
        workspace = self.workspace

        class Observer(NullObserver):
            def on_action(self, action):
                if action.tool == "finish":
                    (workspace / "helper.py").write_text("external = 2\n", encoding="utf-8")

        provider = RecordingProvider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("verify", "run_command", {"command": "python -m compileall -q demo.py"}),
            response("finish", "finish", {"summary": "完成"}),
        ])
        result = CodingAgent(provider, self.tools, plan_enabled=False,
                             observer=Observer()).run("创建文件")
        self.assertFalse(result.ok)
        self.assertEqual("stale", result.task_validation.status)
        self.assertEqual(3, len(provider.histories))

    def test_unknown_script_effect_stops_before_finish(self) -> None:
        provider = RecordingProvider([
            response("create", "create_file", {
                "path": "demo.py", "content": "from pathlib import Path\nPath('extra.txt').write_text('changed')\n",
            }),
            response("script", "run_command", {"command": "python demo.py"}),
            response("finish", "finish", {"summary": "不应执行"}),
        ])
        result = CodingAgent(provider, self.tools, plan_enabled=False).run("创建并运行")
        self.assertFalse(result.ok)
        self.assertTrue(result.unknown_effects)
        self.assertEqual(2, len(provider.histories))

    def test_approval_denial_during_recovery_stops_with_changes_retained(self) -> None:
        self.tools.context.approver = lambda action, detail: action != "run_command"
        provider = RecordingProvider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("finish", "finish", {"summary": "完成"}),
            response("denied", "run_command", {"command": "python -m compileall -q demo.py"}),
        ])
        result = CodingAgent(provider, self.tools, plan_enabled=False).run("创建文件")
        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)
        self.assertEqual("x = 1\n", (self.workspace / "demo.py").read_text("utf-8"))
        self.assertEqual(3, len(provider.histories))

    def test_unrelated_compileall_cannot_complete_after_recovery(self) -> None:
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish-1", "finish", {"summary": "完成", "outcome": "completed"}),
                response(
                    "compile-other",
                    "run_command",
                    {"command": "python -m compileall -q helper.py"},
                ),
                response("finish-2", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=6, plan_enabled=False).run("创建 demo.py")

        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)
        self.assertEqual(4, len(provider.histories))
        # 下一次请求不能收到“已满足”的误导反馈，最终收尾也使用同一范围判断。
        latest = next(message for message in reversed(provider.histories[3])
                      if message.kind == "verification_feedback")
        self.assertEqual("pending", json.loads(latest.content)["verification_obligation"])

    def test_zero_tests_do_not_satisfy_recovery(self) -> None:
        (self.workspace / "empty_tests").mkdir()
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish-1", "finish", {"summary": "完成", "outcome": "completed"}),
                response(
                    "zero-tests",
                    "run_command",
                    {"command": "python -m unittest discover -s empty_tests"},
                ),
                response("finish-2", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=6, plan_enabled=False).run("创建 demo.py")

        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)
        self.assertIn("zero_tests_reported", result.task_validation.records[-1].diagnostics)

    def test_unittest_implicit_discovery_keeps_its_real_workspace_scope(self) -> None:
        """无位置目标的 unittest 确实做默认发现，范围不应丢失为未知。"""
        (self.workspace / "test_demo.py").write_text(
            "import unittest\nfrom demo import x\n"
            "class DemoTests(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(x, 1)\n", encoding="utf-8",
        )
        provider = RecordingProvider([
            response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
            response("tests", "run_command", {"command": "python -m unittest -q"}),
            response("finish", "finish", {"summary": "测试通过"}),
        ])
        result = CodingAgent(provider, self.tools, plan_enabled=False, max_rounds=3).run("创建 demo.py")
        self.assertEqual((".",), result.task_validation.records[-1].targets)
        self.assertTrue(result.ok, result)

    def test_legacy_unknown_obligation_does_not_enter_recovery(self) -> None:
        provider = RecordingProvider(
            [
                response("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                response("finish", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )
        context = SessionContext(verification_obligation="legacy_unknown")

        result = CodingAgent(provider, self.tools, max_rounds=5, plan_enabled=False).run_with_context(
            "创建 demo.py", context
        ).result

        self.assertFalse(result.ok)
        self.assertEqual(2, len(provider.histories))

    def test_new_task_resets_recovery_counter(self) -> None:
        provider = RecordingProvider(
            [
                response("create-a", "create_file", {"path": "a.py", "content": "a = 1\n"}),
                response("finish-a1", "finish", {"summary": "完成 A", "outcome": "completed"}),
                response("finish-a2", "finish", {"summary": "仍完成 A", "outcome": "completed"}),
                response("create-b", "create_file", {"path": "b.py", "content": "b = 1\n"}),
                response("finish-b1", "finish", {"summary": "完成 B", "outcome": "completed"}),
                response(
                    "compile-b",
                    "run_command",
                    {"command": "python -m compileall -q b.py"},
                ),
                response("finish-b2", "finish", {"summary": "完成 B", "outcome": "completed"}),
            ]
        )
        agent = CodingAgent(provider, self.tools, max_rounds=6, plan_enabled=False)

        first = agent.run_with_context("创建 a.py", SessionContext())
        second = agent.run_with_context("创建 b.py", first.context)

        self.assertFalse(first.result.ok)
        self.assertTrue(second.result.ok, second.result)
        recovery_count = sum(
            message.kind == "verification_feedback"
            and "missing_relevant_verification" in (message.content or "")
            for message in second.context.messages
        )
        self.assertEqual(2, recovery_count)

    def test_legacy_protocol_uses_the_same_recovery_rule(self) -> None:
        def legacy(tool: str, arguments: dict[str, object]) -> ProviderResponse:
            return ProviderResponse(
                content=json.dumps(
                    {"tool": tool, "arguments": arguments, "reason": "测试"},
                    ensure_ascii=False,
                )
            )

        provider = RecordingProvider(
            [
                legacy("create_file", {"path": "demo.py", "content": "x = 1\n"}),
                legacy("finish", {"summary": "完成", "outcome": "completed"}),
                legacy("run_command", {"command": "python -m compileall -q demo.py"}),
                legacy("finish", {"summary": "完成并检查", "outcome": "completed"}),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=6,
            plan_enabled=False,
            tool_protocol="legacy_json",
        ).run("创建 demo.py")

        self.assertTrue(result.ok, result)
        self.assertEqual(4, len(provider.histories))

    def test_finish_batch_remaining_action_stays_skipped_and_is_not_replayed(self) -> None:
        provider = RecordingProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall("create", "create_file", {"path": "demo.py", "content": "x = 1\n"}),
                        ToolCall("finish-early", "finish", {"summary": "完成", "outcome": "completed"}),
                        ToolCall(
                            "must-skip",
                            "run_command",
                            {"command": "python -m compileall -q demo.py"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                response(
                    "compile-real",
                    "run_command",
                    {"command": "python -m compileall -q demo.py"},
                ),
                response("finish-final", "finish", {"summary": "完成", "outcome": "completed"}),
            ]
        )

        turn = CodingAgent(provider, self.tools, max_rounds=5, plan_enabled=False).run_with_context(
            "创建 demo.py", SessionContext()
        )

        self.assertTrue(turn.result.ok, turn.result)
        results_by_id = {
            message.tool_call_id: message.content or ""
            for message in turn.context.messages
            if message.kind == "tool_result"
        }
        skipped_payload = json.loads(results_by_id["must-skip"])
        skipped_output = json.loads(skipped_payload["tool_result"]["output"])
        self.assertEqual("skipped", skipped_output["status"])
        self.assertEqual(1, sum(message.tool_call_id == "must-skip" for message in turn.context.messages))
        self.assertEqual(1, sum(message.tool_call_id == "compile-real" for message in turn.context.messages))

    def test_feedback_is_bounded_and_excludes_sensitive_paths(self) -> None:
        state = AgentRunState.start(COMMON_SYSTEM_PROMPT, "修改文件", SessionContext())
        before = checkpoint(state)
        state.verification_required = True
        state.verification = "待验证"
        state.task_modified_files.extend(
            [f"src/{index:02d}-{'x' * 180}.py" for index in range(25)]
        )
        state.task_modified_files.append(".env.local")
        state.task_modified_files.append("C:/private/secret.py")

        encoded = build_batch_feedback(state, before)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        payload = json.loads(encoded)
        self.assertLessEqual(len(encoded), 2_000)
        self.assertLessEqual(len(payload["pending_paths"]), 20)
        self.assertEqual(25, len(payload["pending_paths"]) + payload["omitted_count"])
        self.assertNotIn(".env.local", encoded)
        self.assertNotIn("C:/private", encoded)

    def test_prompt_explains_script_and_finish_recovery_boundaries(self) -> None:
        self.assertIn("普通脚本只记录运行结果", COMMON_SYSTEM_PROMPT)
        self.assertIn("最多纠正一次", COMMON_SYSTEM_PROMPT)
        command = self.tools.describe("run_command")
        finish = self.tools.describe("finish")
        assert command is not None and finish is not None
        self.assertIn("不能解除", command.description)
        self.assertIn("最多反馈一次", finish.description)

    @staticmethod
    def _empty_context():
        return SessionContext()


if __name__ == "__main__":
    unittest.main()
