"""检查事实、交付结论和失败任务记忆的跨层回归。"""

from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.changes import ChangeJournal
from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.context.memory import (
    ConversationMemory,
    MemoryItem,
    TASK_INCOMPLETE_NOTICE,
    TASK_TERMINATION_KIND,
)
from tricoder.context.summarizer import MemorySummaryError, MemorySummaryResult
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.execution_state import ErrorCode
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    ProviderResponse,
    Message,
    SessionContext,
    ToolCall,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.protocols import NativeToolProtocol
from tricoder.session.runtime import (
    ActiveSession,
    MemorySavePreview,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.session.store import SessionStore
from tricoder.tools import ToolContext, ToolRegistry


class _QueueProvider:
    """只返回合成响应；队列耗尽时拒绝隐藏的额外模型请求。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        if not self.responses:
            raise AssertionError("Provider 响应队列已耗尽")
        return self.responses.pop(0)


class _EmptySummarizer:
    """返回空语义字段，验证宿主仍保留可信失败终止事实。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.calls.append(captured)
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=max(message.message_seq or 0 for message in captured),
            ),
            None,
        )


class _TaskRecordingSummarizer:
    """记录每个任务来源，并可在指定调用模拟摘要或校验失败。"""

    def __init__(
        self,
        *,
        fail_calls: set[int] | None = None,
        invalid_calls: set[int] | None = None,
    ) -> None:
        self.calls: list[tuple[Message, ...]] = []
        self.fail_calls = set(fail_calls or ())
        self.invalid_calls = set(invalid_calls or ())

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.calls.append(captured)
        call_number = len(self.calls)
        if call_number in self.fail_calls:
            raise MemorySummaryError("合成摘要失败", code="provider")
        tasks = tuple(message for message in captured if message.kind == "task")
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=(
                    previous.generation + 1
                    if call_number in self.invalid_calls
                    else previous.generation
                ),
                covered_through=max(message.message_seq or 0 for message in captured),
                constraints=tuple(
                    MemoryItem(
                        id=f"constraint-{message.task_id}",
                        text=f"覆盖任务 {message.task_id}",
                        source_ids=(f"m{message.message_seq}",),
                        scope="task",
                        task_id=message.task_id,
                    )
                    for message in tasks
                ),
            ),
            None,
        )


def _finish(call_id: str, *, summary: str = "交付检查报告") -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(ToolCall(call_id, "finish", {"summary": summary}),),
        finish_reason="tool_calls",
    )


def _finish_with_outcome(
    call_id: str,
    outcome: str,
    *,
    summary: str,
) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(
            ToolCall(
                call_id,
                "finish",
                {"summary": summary, "outcome": outcome},
            ),
        ),
        finish_reason="tool_calls",
    )


class TaskOutcomeMemoryReproductionTests(unittest.TestCase):
    """R0：先固定真实失败检查与失败记忆水位的现有缺陷。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / "test_broken.py").write_text(
            "import unittest\n\n"
            "class BrokenTests(unittest.TestCase):\n"
            "    def test_expected_failure(self):\n"
            "        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                approver=lambda _action, _detail: True,
                timeout=10,
            )
        )

    def test_observational_failed_check_can_be_delivered_without_modification_duty(self) -> None:
        """检查失败是报告事实；无文件修改时不能伪造成修改验证义务。"""

        provider = _QueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "run-failing-test",
                            "run_command",
                            {"command": "python -m unittest -v test_broken.py"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                _finish("finish-review"),
            ]
        )
        turn = CodingAgent(
            provider,
            self.tools,
            plan_enabled=False,
            max_rounds=2,
            memory_config=MemoryConfig(compaction="off", persistence="off"),
        ).run_with_context("只运行测试并报告现有失败，不修改文件", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual(2, provider.calls)  # 失败命令后必须重新规划，再显式 finish。
        self.assertEqual((), turn.result.modified_files)
        self.assertEqual("failed", turn.result.task_validation.status)
        self.assertFalse(turn.context.verification_required)
        self.assertGreater(turn.context.latest_completed_task_seq, 0)
        self.assertIn("检查发现失败", turn.result.summary)
        self.assertNotIn("文件修改后的验证失败", turn.result.summary)

    def test_explicit_refresh_consumes_closed_failed_task(self) -> None:
        """异常停止不自动总结，但其闭合历史必须能由显式 refresh 整理。"""

        provider = _QueueProvider(
            [
                ProviderResponse(content="文本一"),
                ProviderResponse(content="文本二"),
                ProviderResponse(content="文本三"),
            ]
        )
        summarizer = _EmptySummarizer()
        agent = CodingAgent(
            provider,
            self.tools,
            plan_enabled=False,
            max_rounds=5,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        )

        failed = agent.run_with_context("故意触发结束协议预算", SessionContext())
        self.assertFalse(failed.result.ok)
        self.assertEqual([], summarizer.calls)
        self.assertGreater(failed.context.latest_completed_task_seq, 0)

        refreshed = agent.refresh_review_memory(failed.context)

        self.assertEqual(1, len(summarizer.calls))
        candidate = refreshed.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(
            failed.context.latest_completed_task_seq,
            candidate.covered_through,
        )
        self.assertTrue(candidate.open_items)

    def test_unpaired_tool_call_still_blocks_ended_task_boundary(self) -> None:
        """通用终止事实不能掩盖真正缺失的工具结果。"""

        call = ToolCall("missing-result", "read_file", {"path": "sample.py"})
        messages = (
            Message(
                "user",
                "用户任务：读取文件",
                kind="task",
                message_seq=1,
                task_id="task-1",
            ),
            Message(
                "assistant",
                None,
                tool_calls=(call,),
                message_seq=2,
                task_id="task-1",
            ),
            Message(
                "user",
                TASK_INCOMPLETE_NOTICE,
                kind=TASK_TERMINATION_KIND,
                message_seq=3,
                task_id="task-1",
            ),
        )
        manager = ContextManager(
            ContextBudget(max_chars=100_000),
            NativeToolProtocol(),
        )

        self.assertIsNone(
            manager.closed_task_boundary(
                messages,
                covered_through=0,
                current_task_id="task-1",
            )
        )
        plan = manager.plan_save_candidate(messages, covered_through=0)
        self.assertFalse(plan.needs_summary)
        self.assertIn("未闭合", plan.reason or "")

    def test_candidate_stitch_accepts_only_trusted_tail_of_containing_task(self) -> None:
        """内部旧边界只能越过所属任务中精确、可信且已配对的终止尾部。"""

        first_call = ToolCall("first-call", "read_file", {"path": "a.py"})
        second_call = ToolCall("second-call", "read_file", {"path": "b.py"})
        first = (
            Message("user", "第一任务", kind="task", message_seq=1, task_id="task-1"),
            Message(
                "assistant", None, kind="tool_call", tool_calls=(first_call,),
                message_seq=2, task_id="task-1",
            ),
            Message(
                "tool", "读取完成", kind="tool_result", tool_call_id=first_call.id,
                message_seq=3, task_id="task-1",
            ),
            Message(
                "user", TASK_INCOMPLETE_NOTICE, kind=TASK_TERMINATION_KIND,
                message_seq=4, task_id="task-1",
            ),
        )
        second = (
            Message("user", "第二任务", kind="task", message_seq=5, task_id="task-5"),
            Message(
                "assistant", None, kind="tool_call", tool_calls=(second_call,),
                message_seq=6, task_id="task-5",
            ),
            Message(
                "tool", "读取完成", kind="tool_result", tool_call_id=second_call.id,
                message_seq=7, task_id="task-5",
            ),
        )
        manager = ContextManager(ContextBudget(max_chars=100_000), NativeToolProtocol())
        previous = ConversationMemory(revision=1, covered_through=3)

        stitched = manager.extend_save_candidate_with_termination(
            previous,
            (*first, *second),
            target=7,
        )

        self.assertIsNotNone(stitched)
        assert stitched is not None
        self.assertEqual(4, stitched.covered_through)
        self.assertEqual(1, len(stitched.open_items))

        ordinary_tail = (
            *first[:3],
            Message("assistant", "普通未覆盖文本", message_seq=4, task_id="task-1"),
            Message(
                "user", TASK_INCOMPLETE_NOTICE, kind=TASK_TERMINATION_KIND,
                message_seq=5, task_id="task-1",
            ),
            Message("user", "第二任务", kind="task", message_seq=6, task_id="task-6"),
            Message(
                "assistant", None, kind="tool_call", tool_calls=(second_call,),
                message_seq=7, task_id="task-6",
            ),
            Message(
                "tool", "读取完成", kind="tool_result", tool_call_id=second_call.id,
                message_seq=8, task_id="task-6",
            ),
        )
        self.assertIsNone(
            manager.extend_save_candidate_with_termination(
                previous,
                ordinary_tail,
                target=8,
            )
        )

        wrong_source_tail = (
            *first[:3],
            Message(
                "user", TASK_INCOMPLETE_NOTICE, kind=TASK_TERMINATION_KIND,
                message_seq=4, task_id="other-task",
            ),
            *second,
        )
        self.assertIsNone(
            manager.extend_save_candidate_with_termination(
                previous,
                wrong_source_tail,
                target=7,
            )
        )

        dangling_call = ToolCall("dangling", "read_file", {"path": "missing.py"})
        dangling = (
            Message("user", "损坏任务", kind="task", message_seq=1, task_id="task-1"),
            Message(
                "assistant", None, kind="tool_call", tool_calls=(dangling_call,),
                message_seq=2, task_id="task-1",
            ),
            Message(
                "user", TASK_INCOMPLETE_NOTICE, kind=TASK_TERMINATION_KIND,
                message_seq=3, task_id="task-1",
            ),
            Message("user", "第二任务", kind="task", message_seq=4, task_id="task-4"),
            Message(
                "assistant", None, kind="tool_call", tool_calls=(second_call,),
                message_seq=5, task_id="task-4",
            ),
            Message(
                "tool", "读取完成", kind="tool_result", tool_call_id=second_call.id,
                message_seq=6, task_id="task-4",
            ),
        )
        self.assertIsNone(
            manager.extend_save_candidate_with_termination(
                ConversationMemory(revision=1, covered_through=2),
                dangling,
                target=6,
            )
        )


class FinishOutcomeContractTests(unittest.TestCase):
    """R1：finish 的交付声明与宿主检查事实保持独立。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                approver=lambda _action, _detail: True,
            )
        )

    def test_finish_schema_accepts_completed_and_incomplete_but_rejects_unknown(self) -> None:
        definition = next(item for item in self.tools.definitions if item.name == "finish")
        self.assertEqual(
            ["completed", "incomplete"],
            definition.parameters["properties"]["outcome"]["enum"],
        )
        self.assertEqual(["summary"], definition.parameters["required"])

        default = self.tools.execute("finish", {"summary": "默认完成"})
        completed = self.tools.execute(
            "finish", {"summary": "明确完成", "outcome": "completed"}
        )
        incomplete = self.tools.execute(
            "finish", {"summary": "仍有阻塞", "outcome": "incomplete"}
        )
        invalid = self.tools.execute(
            "finish", {"summary": "非法", "outcome": "unknown"}
        )

        self.assertTrue(default.ok)
        self.assertTrue(completed.ok)
        self.assertTrue(incomplete.ok)
        self.assertFalse(invalid.ok)
        self.assertIsNotNone(invalid.error)
        assert invalid.error is not None
        self.assertIs(ErrorCode.INVALID_ARGUMENT, invalid.error.code)

    def test_native_incomplete_finishes_protocol_but_run_stays_unsuccessful(self) -> None:
        provider = _QueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "finish-incomplete",
                            "finish",
                            {"summary": "尚未解决", "outcome": "incomplete"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            plan_enabled=False,
            max_rounds=1,
            memory_config=MemoryConfig(compaction="off", persistence="off"),
        ).run_with_context("无法完成的任务", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("尚未解决", turn.result.summary)
        self.assertGreater(turn.context.latest_completed_task_seq, 0)
        self.assertEqual("task_termination", turn.context.messages[-1].kind)

    def test_legacy_incomplete_uses_same_outcome_contract(self) -> None:
        provider = _QueueProvider(
            [
                ProviderResponse(
                    content=json.dumps(
                        {
                            "tool": "finish",
                            "arguments": {
                                "summary": "legacy 尚未完成",
                                "outcome": "incomplete",
                            },
                            "reason": "报告阻塞",
                        },
                        ensure_ascii=False,
                    )
                )
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            tool_protocol="legacy_json",
            plan_enabled=False,
            max_rounds=1,
            memory_config=MemoryConfig(compaction="off", persistence="off"),
        ).run_with_context("legacy 无法完成", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("legacy 尚未完成", turn.result.summary)

    def test_incomplete_finish_builds_pending_memory_without_becoming_success(self) -> None:
        summarizer = _EmptySummarizer()
        turn = CodingAgent(
            _QueueProvider(
                [
                    _finish_with_outcome(
                        "finish-blocked",
                        "incomplete",
                        summary="仍被外部条件阻塞",
                    )
                ]
            ),
            self.tools,
            plan_enabled=False,
            max_rounds=1,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        ).run_with_context("处理受阻任务", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual(1, len(summarizer.calls))
        candidate = turn.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(turn.context.latest_completed_task_seq, candidate.covered_through)
        self.assertEqual(1, len(candidate.open_items))
        self.assertIn("未成功结束", candidate.open_items[0].text)

    def test_completed_cannot_bypass_unverified_modification_and_failure_is_remembered(self) -> None:
        (self.workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
        summarizer = _EmptySummarizer()
        provider = _QueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "edit-sample",
                            "edit_file",
                            {
                                "path": "sample.py",
                                "old_text": "value = 1",
                                "new_text": "value = 2",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                _finish("finish-unverified", summary="声称已经完成"),
            ]
        )

        turn = CodingAgent(
            provider,
            self.tools,
            plan_enabled=False,
            max_rounds=2,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        ).run_with_context("修改 sample.py", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("待验证", turn.result.verification)
        self.assertEqual(("sample.py",), turn.result.modified_files)
        self.assertEqual(1, len(summarizer.calls))
        candidate = turn.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(1, len(candidate.open_items))

    def test_failed_then_successful_tasks_keep_contiguous_memory_coverage(self) -> None:
        summarizer = _EmptySummarizer()
        provider = _QueueProvider(
            [
                _finish_with_outcome(
                    "finish-first",
                    "incomplete",
                    summary="第一项未完成",
                ),
                _finish("finish-second", summary="第二项已交付"),
            ]
        )
        agent = CodingAgent(
            provider,
            self.tools,
            plan_enabled=False,
            max_rounds=1,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        )

        first = agent.run_with_context("第一项", SessionContext())
        second = agent.run_with_context("第二项", first.context)

        self.assertFalse(first.result.ok)
        self.assertTrue(second.result.ok, second.result.summary)
        self.assertEqual(2, len(summarizer.calls))
        candidate = second.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(second.context.latest_completed_task_seq, candidate.covered_through)
        self.assertTrue(candidate.open_items)


class EmptyMemoryPreviewReproductionTests(unittest.TestCase):
    """初始 revision=0/coverage=0 不是用户确认过的可保存空记忆。"""

    def test_initial_empty_memory_candidate_is_not_saveable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            store = SessionStore(database, id_factory=lambda: "empty-memory")
            store.initialize(workspace)
            record = store.create("empty", workspace, "openai", "test")
            config = AppConfig(
                workspace=workspace,
                provider=ProviderConfig(
                    "openai",
                    "synthetic-test-key",
                    "https://api.openai.com/v1",
                    "test",
                ),
                audit_dir=(root / "audit").resolve(),
                memory=MemoryConfig(
                    compaction="structured",
                    persistence="reviewed_summary",
                ),
            )

            def factory(session_record, memory, options):  # type: ignore[no-untyped-def]
                return ActiveSession(
                    session_record,
                    memory,
                    SessionContext(),
                    config,
                    object(),
                )

            runtime = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=factory,
                initial_session_id=record.id,
                workspace_confirmer=lambda _preview: True,
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "暂无可保存"):
                    runtime.preview_memory_save()
            finally:
                runtime.close()


class RuntimeFinalOutcomeMemoryTests(unittest.TestCase):
    """F2：Runtime 在 Agent 返回后改判失败时也要闭合历史和失效旧候选。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.workspace = (root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.database = (root / "state" / "sessions.db").resolve()
        store = SessionStore(self.database, id_factory=lambda: "runtime-final-memory")
        store.initialize(self.workspace)
        self.record = store.create(
            "runtime-final-memory", self.workspace, "openai", "test"
        )
        self.config = AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                "openai",
                "synthetic-test-key",
                "https://api.openai.com/v1",
                "test",
            ),
            audit_dir=(root / "audit").resolve(),
            memory=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
        )

    def _runtime(
        self,
        provider: _QueueProvider,
        summarizer: _EmptySummarizer,
    ) -> SessionRuntime:
        def factory(record, memory, options):  # type: ignore[no-untyped-def]
            journal = ChangeJournal()
            tools = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(self.workspace),
                    CommandPolicy(self.workspace),
                    approver=lambda _action, _detail: True,
                    change_journal=journal,
                )
            )
            agent = CodingAgent(
                provider,
                tools,
                plan_enabled=False,
                max_rounds=2,
                memory_config=self.config.memory,
                memory_summarizer=summarizer,
            )
            return ActiveSession(
                record,
                memory,
                SessionContext(
                    persisted_summary=memory.summary,
                    modified_files=memory.modified_files,
                    verification=memory.verification,
                ),
                self.config,
                agent,
                tools,
                journal,
            )

        runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=factory,
            initial_session_id=self.record.id,
            workspace_confirmer=lambda _preview: True,
        )
        self.addCleanup(runtime.close)
        return runtime

    @staticmethod
    def _preview_before_runtime_denial(
        runtime: SessionRuntime,
        context: SessionContext,
    ) -> MemorySavePreview:
        candidate = context.review_memory_candidate
        memory = context.conversation_memory
        assert isinstance(candidate, ConversationMemory)
        assert isinstance(memory, ConversationMemory)
        return MemorySavePreview(
            session_id=runtime.current.record.id,
            generation=memory.generation,
            revision=memory.revision,
            persisted_revision=context.persisted_memory_revision,
            next_message_seq=context.next_message_seq,
            text="旧保存预览",
            original_memory=memory,
            original_review_candidate=candidate,
            candidate=candidate,
        )

    def test_runtime_final_scan_denial_is_refreshable_saveable_and_restartable(self) -> None:
        """最终扫描否决必须进入失败记忆，且 refresh 不重跑业务工具。"""

        provider = _QueueProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "check-app",
                            "run_command",
                            {"command": "python -m compileall -q app.py"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                _finish("finish-review"),
            ]
        )
        summarizer = _EmptySummarizer()
        runtime = self._runtime(provider, summarizer)
        agent = runtime.current.agent
        original_run = agent.run_with_context
        captured: dict[str, object] = {}

        def late_change(task, context, **kwargs):  # type: ignore[no-untyped-def]
            turn = original_run(task, context, **kwargs)
            captured["candidate"] = turn.context.review_memory_candidate
            captured["preview"] = self._preview_before_runtime_denial(runtime, turn.context)
            (self.workspace / "app.py").write_text("x = 999\n", encoding="utf-8")
            return turn

        agent.run_with_context = late_change

        result = runtime.run_task("只审查 app.py，不修改文件")

        self.assertFalse(result.ok)
        context = runtime.current.context
        self.assertEqual(
            1,
            sum(message.kind == TASK_TERMINATION_KIND for message in context.messages),
        )
        self.assertGreater(context.latest_completed_task_seq, 0)
        self.assertIs(context.review_memory_candidate, captured["candidate"])
        assert isinstance(context.review_memory_candidate, ConversationMemory)
        self.assertLess(
            context.review_memory_candidate.covered_through,
            context.latest_completed_task_seq,
        )
        with self.assertRaisesRegex(SessionRuntimeError, "请先运行 /memory refresh"):
            runtime.preview_memory_save()
        with self.assertRaisesRegex(SessionRuntimeError, "确认期间已变化"):
            runtime.save_memory_preview(captured["preview"])

        calls_before_refresh = provider.calls
        with patch.object(runtime.current.tools, "execute", wraps=runtime.current.tools.execute) as execute:
            refreshed = runtime.refresh_memory()
        execute.assert_not_called()
        self.assertEqual(calls_before_refresh, provider.calls)
        candidate = refreshed.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(
            refreshed.context.latest_completed_task_seq,
            candidate.covered_through,
        )
        self.assertTrue(candidate.open_items)
        self.assertIn("未成功结束", candidate.open_items[-1].text)

        preview = runtime.preview_memory_save()
        runtime.save_memory_preview(preview)
        runtime.close()

        restarted = self._runtime(_QueueProvider([]), _EmptySummarizer())
        restored = restarted.current.context
        self.assertTrue(restored.conversation_memory.open_items)
        self.assertIn(
            "未成功结束",
            restored.conversation_memory.open_items[-1].text,
        )
        self.assertEqual((), restored.messages)
        self.assertIsNone(restored.verification_evidence)

    def test_runtime_cancel_and_cleanup_denials_do_not_resummarize_or_duplicate_marker(self) -> None:
        """取消、清理失败只补一次宿主终止事实，不增加摘要请求。"""

        from tricoder.task_cleanup import current_cleanup

        for mode in ("cancel", "cleanup"):
            with self.subTest(mode=mode):
                provider = _QueueProvider([_finish(f"finish-{mode}")])
                summarizer = _EmptySummarizer()
                runtime = self._runtime(provider, summarizer)
                try:
                    agent = runtime.current.agent
                    original_run = agent.run_with_context

                    def deny_after_return(task, context, **kwargs):  # type: ignore[no-untyped-def]
                        turn = original_run(task, context, **kwargs)
                        if mode == "cancel":
                            runtime.current_task_cancellation().cancel()
                        else:
                            current_cleanup().mark_failed()
                        return turn

                    agent.run_with_context = deny_after_return

                    result = runtime.run_task(f"{mode} after Agent return")

                    self.assertFalse(result.ok)
                    self.assertEqual(1, provider.calls)
                    self.assertEqual(1, len(summarizer.calls))
                    self.assertEqual(
                        1,
                        sum(
                            message.kind == TASK_TERMINATION_KIND
                            for message in runtime.current.context.messages
                        ),
                    )
                finally:
                    runtime.close()

    def test_late_cancel_then_next_task_repairs_candidate_automatically(self) -> None:
        """旧任务迟到终止事实应先本地补齐，再摘要后续完整任务。"""

        provider = _QueueProvider([_finish("first"), _finish("second")])
        summarizer = _TaskRecordingSummarizer()
        runtime = self._runtime(provider, summarizer)  # type: ignore[arg-type]
        agent = runtime.current.agent
        original_run = agent.run_with_context

        def late_cancel(task, context, **kwargs):  # type: ignore[no-untyped-def]
            turn = original_run(task, context, **kwargs)
            runtime.current_task_cancellation().cancel()
            return turn

        agent.run_with_context = late_cancel
        first = runtime.run_task("第一项只读审查")
        stale_context = runtime.current.context
        stale_candidate = stale_context.review_memory_candidate
        stale_preview = self._preview_before_runtime_denial(runtime, stale_context)
        agent.run_with_context = original_run

        second = runtime.run_task("第二项只读审查")

        self.assertFalse(first.ok)
        self.assertTrue(second.ok, second.summary)
        context = runtime.current.context
        candidate = context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(context.latest_completed_task_seq, candidate.covered_through)
        self.assertEqual(2, len(candidate.constraints))
        self.assertEqual(1, len(candidate.open_items))
        self.assertIn("未成功结束", candidate.open_items[0].text)
        self.assertEqual(2, len(summarizer.calls))
        self.assertEqual(1, sum(message.kind == "task" for message in summarizer.calls[-1]))
        self.assertTrue(all(
            message.message_seq is None
            or message.message_seq > stale_candidate.covered_through
            for message in summarizer.calls[-1]
        ))
        calls_before_refresh = len(summarizer.calls)
        refreshed = runtime.refresh_memory()
        self.assertEqual(calls_before_refresh, len(summarizer.calls))
        self.assertEqual(candidate, refreshed.context.review_memory_candidate)
        with self.assertRaisesRegex(SessionRuntimeError, "确认期间已变化"):
            runtime.save_memory_preview(stale_preview)

        preview = runtime.preview_memory_save()
        runtime.save_memory_preview(preview)
        runtime.close()
        restarted = self._runtime(_QueueProvider([]), _EmptySummarizer())
        restored = restarted.current.context.conversation_memory
        self.assertEqual(2, len(restored.constraints))
        self.assertEqual(1, len(restored.open_items))

    def test_final_scan_denial_then_next_task_uses_workspace_gate_and_repairs_memory(self) -> None:
        """最终扫描否决后的下一任务仍须确认变化并重新取得验证证据。"""

        def check(call_id: str) -> ProviderResponse:
            return ProviderResponse(
                tool_calls=(ToolCall(
                    call_id,
                    "run_command",
                    {"command": "python -m compileall -q app.py"},
                ),),
                finish_reason="tool_calls",
            )
        provider = _QueueProvider([
            check("check-first"),
            _finish("finish-first"),
            check("check-second"),
            _finish("finish-second"),
        ])
        summarizer = _TaskRecordingSummarizer()
        runtime = self._runtime(provider, summarizer)  # type: ignore[arg-type]
        confirmations = []
        runtime._workspace_confirmer = lambda preview: confirmations.append(preview) or True
        agent = runtime.current.agent
        original_run = agent.run_with_context

        def late_change(task, context, **kwargs):  # type: ignore[no-untyped-def]
            turn = original_run(task, context, **kwargs)
            (self.workspace / "app.py").write_text("x = 999\n", encoding="utf-8")
            return turn

        agent.run_with_context = late_change
        first = runtime.run_task("审查当前 app.py")
        agent.run_with_context = original_run
        second = runtime.run_task("确认变化后重新检查 app.py")

        self.assertFalse(first.ok)
        self.assertTrue(second.ok, second.summary)
        self.assertTrue(
            any(preview.kind == "changed" for preview in confirmations),
            [preview.kind for preview in confirmations],
        )
        self.assertEqual([0], [record.returncode for record in second.task_validation.records])
        self.assertIsNotNone(runtime.current.context.verification_evidence)
        candidate = runtime.current.context.review_memory_candidate
        self.assertEqual(
            runtime.current.context.latest_completed_task_seq,
            candidate.covered_through,
        )
        self.assertEqual(1, len(candidate.open_items))
        self.assertEqual(2, len(candidate.constraints))

    def test_stale_multi_task_candidate_refresh_is_atomic_and_recoverable(self) -> None:
        """已有多任务坏状态可刷新恢复；失败、校验拒绝和取消不发布半成品。"""

        provider = _QueueProvider([_finish("first"), _finish("second")])
        automatic = _TaskRecordingSummarizer(fail_calls={2})
        runtime = self._runtime(provider, automatic)  # type: ignore[arg-type]
        agent = runtime.current.agent
        original_run = agent.run_with_context

        def late_cancel(task, context, **kwargs):  # type: ignore[no-untyped-def]
            turn = original_run(task, context, **kwargs)
            runtime.current_task_cancellation().cancel()
            return turn

        agent.run_with_context = late_cancel
        runtime.run_task("第一项只读审查")
        stale_preview = self._preview_before_runtime_denial(runtime, runtime.current.context)
        stale_candidate = runtime.current.context.review_memory_candidate
        agent.run_with_context = original_run
        second = runtime.run_task("第二项只读审查")

        self.assertTrue(second.ok)
        self.assertIn("候选生成失败", second.summary)
        self.assertIs(stale_candidate, runtime.current.context.review_memory_candidate)
        stale_context = runtime.current.context

        cancelled = CancellationToken()
        cancelled.cancel()
        with self.assertRaises(CancellationError):
            agent.refresh_review_memory(stale_context, cancellation=cancelled)
        self.assertEqual(stale_context, runtime.current.context)

        invalid = _TaskRecordingSummarizer(invalid_calls={1})
        agent.memory_summarizer = invalid
        with self.assertRaisesRegex(SessionRuntimeError, "刷新结果无效"):
            runtime.refresh_memory()
        self.assertEqual(stale_context, runtime.current.context)

        retry = _TaskRecordingSummarizer()
        agent.memory_summarizer = retry
        with patch.object(runtime.current.tools, "execute", wraps=runtime.current.tools.execute) as execute:
            refreshed = runtime.refresh_memory()
        execute.assert_not_called()
        candidate = refreshed.context.review_memory_candidate
        self.assertEqual(refreshed.context.latest_completed_task_seq, candidate.covered_through)
        self.assertEqual(2, len(candidate.constraints))
        self.assertEqual(1, len(candidate.open_items))
        self.assertEqual(1, len(retry.calls))
        with self.assertRaisesRegex(SessionRuntimeError, "确认期间已变化"):
            runtime.save_memory_preview(stale_preview)
        preview = runtime.preview_memory_save()
        runtime.save_memory_preview(preview)

    def test_failed_refresh_save_and_restart_restores_pending_fact_only(self) -> None:
        """异常失败经显式刷新和确认保存后，只恢复低信任未完成事项。"""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            store = SessionStore(database, id_factory=lambda: "failed-memory")
            store.initialize(workspace)
            record = store.create("failed", workspace, "openai", "test")
            config = AppConfig(
                workspace=workspace,
                provider=ProviderConfig(
                    "openai",
                    "synthetic-test-key",
                    "https://api.openai.com/v1",
                    "test",
                ),
                audit_dir=(root / "audit").resolve(),
                memory=MemoryConfig(
                    compaction="structured",
                    persistence="reviewed_summary",
                ),
            )
            tools = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(workspace),
                    CommandPolicy(workspace),
                    approver=lambda _action, _detail: True,
                )
            )
            summarizer = _EmptySummarizer()
            agent = CodingAgent(
                _QueueProvider(
                    [
                        ProviderResponse(content="文本一"),
                        ProviderResponse(content="文本二"),
                        ProviderResponse(content="文本三"),
                    ]
                ),
                tools,
                plan_enabled=False,
                max_rounds=5,
                memory_config=config.memory,
                memory_summarizer=summarizer,
            )
            failed = agent.run_with_context("触发异常停止", SessionContext())
            refreshed = agent.refresh_review_memory(failed.context)

            def first_factory(session_record, memory, options):  # type: ignore[no-untyped-def]
                return ActiveSession(
                    session_record,
                    memory,
                    refreshed.context,
                    config,
                    object(),
                )

            runtime = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=first_factory,
                initial_session_id=record.id,
                workspace_confirmer=lambda _preview: True,
            )
            preview = runtime.preview_memory_save()
            runtime.save_memory_preview(preview)
            runtime.close()

            def restart_factory(session_record, memory, options):  # type: ignore[no-untyped-def]
                return ActiveSession(
                    session_record,
                    memory,
                    SessionContext(
                        verification="通过",
                        verification_required=False,
                    ),
                    config,
                    object(),
                )

            restarted = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=restart_factory,
                initial_session_id=record.id,
                workspace_confirmer=lambda _preview: True,
            )
            try:
                restored = restarted.current.context
                self.assertTrue(restored.conversation_memory.open_items)
                self.assertIn(
                    "结束协议",
                    restored.conversation_memory.open_items[0].text,
                )
                self.assertIsNone(restored.verification_evidence)
                self.assertFalse(restored.verification_required)
                self.assertEqual((), restored.messages)
            finally:
                restarted.close()

    def test_explicitly_edited_empty_memory_remains_saveable(self) -> None:
        """revision>0 代表显式编辑/清理结果，不能被初始空对象规则误挡。"""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            store = SessionStore(database, id_factory=lambda: "edited-empty")
            store.initialize(workspace)
            record = store.create("edited", workspace, "openai", "test")
            config = AppConfig(
                workspace=workspace,
                provider=ProviderConfig(
                    "openai",
                    "synthetic-test-key",
                    "https://api.openai.com/v1",
                    "test",
                ),
                audit_dir=(root / "audit").resolve(),
                memory=MemoryConfig(
                    compaction="structured",
                    persistence="reviewed_summary",
                ),
            )
            edited = ConversationMemory(revision=1)

            def factory(session_record, memory, options):  # type: ignore[no-untyped-def]
                return ActiveSession(
                    session_record,
                    memory,
                    SessionContext(conversation_memory=edited),
                    config,
                    object(),
                )

            runtime = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=factory,
                initial_session_id=record.id,
                workspace_confirmer=lambda _preview: True,
            )
            try:
                preview = runtime.preview_memory_save()
                self.assertEqual(edited, preview.candidate)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
