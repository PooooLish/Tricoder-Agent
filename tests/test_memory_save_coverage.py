from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tricoder.agent import CodingAgent
from tricoder.context.memory import ConversationMemory, MemoryItem
from tricoder.context.summarizer import MemorySummaryError, MemorySummaryResult
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    Message,
    ProviderConfig,
    ProviderResponse,
    SessionContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tricoder.session_runtime import ActiveSession, RuntimeOptions, SessionRuntime
from tricoder.sessions import SessionStore


class _FinishProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ProviderResponse(
            tool_calls=(
                ToolCall(f"finish-{self.calls}", "finish", {"summary": "完成"}),
            ),
            finish_reason="tool_calls",
        )


class _FinishTools:
    definitions = (ToolDefinition("finish", "结束", {"type": "object"}),)

    @staticmethod
    def contains(name: str) -> bool:
        return name == "finish"

    @staticmethod
    def describe(name: str):  # type: ignore[no-untyped-def]
        return _FinishTools.definitions[0] if name == "finish" else None

    @staticmethod
    def requires_approval(name: str) -> bool:
        return False

    @staticmethod
    def execute(name: str, arguments: dict[str, object], **kwargs):  # type: ignore[no-untyped-def]
        return ToolResult(True, str(arguments.get("summary", "")))


class _TaskAwareSummarizer:
    """按任务标记生成确定性候选，不访问网络或文件。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.calls.append(captured)
        tasks = [message for message in captured if message.kind == "task"]
        covered = max(message.message_seq or 0 for message in captured)
        constraints = tuple(
            MemoryItem(
                f"constraint-{message.task_id}",
                f"保留 {message.task_id}",
                (f"m{message.message_seq}",),
                "task",
                message.task_id,
            )
            for message in tasks
        )
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=covered,
                constraints=constraints,
            ),
            None,
        )


class _BoundedTaskSummarizer(_TaskAwareSummarizer):
    """模拟真实摘要器的输入预检，每批最多接收指定数量的任务。"""

    def __init__(self, max_tasks: int) -> None:
        super().__init__()
        self.max_tasks = max_tasks

    def source_input_fits(self, previous, source):  # type: ignore[no-untyped-def]
        return sum(message.kind == "task" for message in source) <= self.max_tasks


class _FailSecondSummarizer(_TaskAwareSummarizer):
    """首个任务生成候选，第二个任务模拟收尾摘要失败。"""

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        if len(self.calls) == 1:
            self.calls.append(tuple(source))
            raise MemorySummaryError("合成故障", code="provider")
        return await super().summarize(previous, source, cancellation)


def _reviewed_agent(summarizer: _TaskAwareSummarizer) -> CodingAgent:
    return CodingAgent(
        _FinishProvider(),
        _FinishTools(),
        plan_enabled=False,
        max_context_chars=100_000,
        memory_config=MemoryConfig(
            compaction="structured",
            persistence="reviewed_summary",
        ),
        memory_summarizer=summarizer,
    )


class MemorySaveCoverageTests(unittest.TestCase):
    def test_single_completed_task_builds_save_candidate_without_deleting_history(self) -> None:
        """一个已完成任务也必须进入保存候选，近期原始消息继续留在内存。"""

        summarizer = _TaskAwareSummarizer()
        turn = _reviewed_agent(summarizer).run_with_context(
            "第一个任务",
            SessionContext(),
        )

        self.assertTrue(turn.result.ok)
        self.assertEqual(1, len(summarizer.calls))
        self.assertEqual(ConversationMemory(), turn.context.conversation_memory)
        candidate = turn.context.review_memory_candidate
        self.assertIsNotNone(candidate)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(1, len(candidate.constraints))
        self.assertTrue(any(message.kind == "task" for message in turn.context.messages))

    def test_second_completed_task_extends_candidate_without_resummarizing_first(self) -> None:
        """连续任务只总结保存边界之后的新任务，并覆盖最新已完成任务。"""

        summarizer = _TaskAwareSummarizer()
        agent = _reviewed_agent(summarizer)
        first = agent.run_with_context("任务 A", SessionContext())

        second = agent.run_with_context("任务 B", first.context)

        self.assertTrue(second.result.ok)
        self.assertEqual(2, len(summarizer.calls))
        self.assertEqual(
            1,
            sum(message.kind == "task" for message in summarizer.calls[1]),
        )
        candidate = second.context.review_memory_candidate
        self.assertIsNotNone(candidate)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(2, len(candidate.constraints))
        self.assertEqual(2, sum(message.kind == "task" for message in second.context.messages))
        self.assertEqual(ConversationMemory(), second.context.conversation_memory)

    def test_save_candidate_uses_at_most_two_complete_task_batches(self) -> None:
        """整段输入超限时按完整任务拆为至多两批，全部成功后才发布候选。"""

        summarizer = _BoundedTaskSummarizer(max_tasks=2)
        agent = _reviewed_agent(summarizer)
        context = SessionContext()
        for task in ("任务 A", "任务 B", "任务 C"):
            # 前两轮先使用较大的单批上限，第三轮再触发三任务整体超限。
            if task == "任务 C":
                summarizer.max_tasks = 2
            context = agent.run_with_context(task, context).context

        self.assertLessEqual(len(summarizer.calls), 4)
        # 每次任务只处理保存边界之后的新内容；直接构造积压场景验证双批。
        backlog = SessionContext(
            messages=context.messages,
            next_message_seq=context.next_message_seq,
        )
        fresh = _BoundedTaskSummarizer(max_tasks=2)
        covered = _reviewed_agent(fresh).run_with_context("任务 D", backlog)

        self.assertTrue(covered.result.ok)
        self.assertEqual(2, len(fresh.calls))
        self.assertTrue(all(
            sum(message.kind == "task" for message in call) <= 2
            for call in fresh.calls
        ))
        candidate = covered.context.review_memory_candidate
        self.assertIsNotNone(candidate)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual(4, len(candidate.constraints))

    def test_more_than_two_required_batches_refuses_incomplete_save_candidate(self) -> None:
        """两批仍放不下时保留原状态，不发布标称完整的候选。"""

        seed_summarizer = _TaskAwareSummarizer()
        seed_agent = _reviewed_agent(seed_summarizer)
        context = SessionContext()
        for task in ("任务 A", "任务 B", "任务 C"):
            context = seed_agent.run_with_context(task, context).context
        backlog = replace(context, review_memory_candidate=None)
        limited = _BoundedTaskSummarizer(max_tasks=1)

        turn = _reviewed_agent(limited).run_with_context("任务 D", backlog)

        self.assertTrue(turn.result.ok)
        self.assertIsNone(turn.context.review_memory_candidate)
        self.assertEqual([], limited.calls)
        self.assertIn("完整保存", turn.memory_warning)


class MemorySavePreviewCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        store = SessionStore(self.database, id_factory=lambda: "session-save")
        store.initialize(self.workspace)
        store.create("save", self.workspace, "openai", "test")
        config = AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                "openai",
                "synthetic-test-key",
                "https://api.openai.com/v1",
                "test",
            ),
            audit_dir=(self.root / "audit").resolve(),
            memory=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
        )

        def factory(record, memory, options):  # type: ignore[no-untyped-def]
            return ActiveSession(record, memory, SessionContext(), config, object())

        self.runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=factory,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_saves_review_candidate_but_keeps_recent_runtime_history(self) -> None:
        """确认保存独立候选；当前运行时摘要和近期原始消息不被替换。"""

        runtime_memory = ConversationMemory()
        candidate = ConversationMemory(
            revision=1,
            covered_through=3,
            constraints=(
                MemoryItem("latest", "覆盖最新任务", ("m1",), "session"),
            ),
        )
        messages = (
            Message("user", "任务", kind="task", message_seq=1, task_id="task-1"),
            Message("assistant", "处理中", message_seq=2, task_id="task-1"),
            Message("assistant", "完成", message_seq=3, task_id="task-1"),
        )
        self.runtime.current = replace(
            self.runtime.current,
            context=replace(
                self.runtime.current.context,
                messages=messages,
                conversation_memory=runtime_memory,
                review_memory_candidate=candidate,
                next_message_seq=4,
                latest_completed_task_seq=3,
            ),
        )

        preview = self.runtime.preview_memory_save()
        self.assertEqual(candidate, preview.candidate)
        self.assertEqual(runtime_memory, preview.original_memory)
        self.assertIn("覆盖状态：完整", preview.text)
        self.runtime.save_memory_preview(preview)

        self.assertEqual(runtime_memory, self.runtime.current.context.conversation_memory)
        self.assertEqual(messages, self.runtime.current.context.messages)
        self.assertEqual(
            (candidate, 4),
            self.runtime.store.load_conversation_memory(
                self.runtime.current.record.id
            ),
        )
        restarted = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=lambda record, memory, options: ActiveSession(
                record,
                memory,
                SessionContext(),
                self.runtime.current.config,
                object(),
            ),
        )
        self.assertEqual(
            candidate,
            restarted.current.context.conversation_memory,
        )
        self.assertIsNone(restarted.current.context.review_memory_candidate)
        self.assertEqual((), restarted.current.context.messages)

    def test_second_task_summary_failure_blocks_saving_stale_candidate(self) -> None:
        """修复目标：最新成功任务未进候选时，普通保存必须拒绝旧候选。"""

        summarizer = _FailSecondSummarizer()
        agent = _reviewed_agent(summarizer)
        first = agent.run_with_context("任务 A", SessionContext())
        second = agent.run_with_context("任务 B", first.context)
        stale = second.context.review_memory_candidate
        self.assertTrue(second.result.ok)
        self.assertIsNotNone(stale)
        assert isinstance(stale, ConversationMemory)
        self.assertLess(
            stale.covered_through,
            max(message.message_seq or 0 for message in second.context.messages),
        )
        self.runtime.current = replace(self.runtime.current, context=second.context)

        rendered = self.runtime.render_memory()
        self.assertIn("候选覆盖不足", rendered)
        self.assertIn(str(stale.covered_through), rendered)
        self.assertIn(str(second.context.latest_completed_task_seq), rendered)

        with self.assertRaisesRegex(RuntimeError, "覆盖|刷新"):
            self.runtime.preview_memory_save()
        self.assertIsNone(
            self.runtime.store.load_conversation_memory(
                self.runtime.current.record.id
            )
        )


if __name__ == "__main__":
    unittest.main()
