from __future__ import annotations

import threading
import unittest
from dataclasses import replace
import tempfile
from pathlib import Path

from tricoder.agent import CodingAgent
from tricoder.context.memory import ConversationMemory, MemoryItem
from tricoder.context.summarizer import MemorySummaryError, MemorySummaryResult
from tricoder.models import (
    AppConfig,
    MemoryRefreshResult,
    MemoryConfig,
    ProviderConfig,
    ProviderResponse,
    SessionContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tricoder.session_runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
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


class _TaskSummarizer:
    def __init__(self, *, max_tasks: int | None = None, fail_call: int | None = None) -> None:
        self.max_tasks = max_tasks
        self.fail_call = fail_call
        self.calls: list[tuple[object, ...]] = []

    def source_input_fits(self, previous, source):  # type: ignore[no-untyped-def]
        if self.max_tasks is None:
            return True
        return sum(message.kind == "task" for message in source) <= self.max_tasks

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.calls.append(captured)
        if self.fail_call == len(self.calls):
            raise MemorySummaryError("合成刷新故障", code="provider")
        tasks = [message for message in captured if message.kind == "task"]
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=max(message.message_seq or 0 for message in captured),
                constraints=tuple(
                    MemoryItem(
                        f"constraint-{message.task_id}",
                        f"保留 {message.task_id}",
                        (f"m{message.message_seq}",),
                        "task",
                        message.task_id,
                    )
                    for message in tasks
                ),
            ),
            None,
        )


def _agent(
    provider: _FinishProvider,
    summarizer: _TaskSummarizer,
    *,
    persistence: str = "reviewed_summary",
) -> CodingAgent:
    return CodingAgent(
        provider,
        _FinishTools(),
        plan_enabled=False,
        max_context_chars=100_000,
        memory_config=MemoryConfig(
            compaction="structured" if persistence != "off" else "off",
            persistence=persistence,
        ),
        memory_summarizer=summarizer,
    )


class MemoryRefreshTests(unittest.TestCase):
    def test_refresh_retries_stale_candidate_without_business_round_or_history_loss(self) -> None:
        """刷新只请求摘要器，不新增业务轮次、工具调用或删除近期历史。"""

        provider = _FinishProvider()
        initial = _TaskSummarizer(fail_call=2)
        agent = _agent(provider, initial)
        first = agent.run_with_context("任务 A", SessionContext())
        second = agent.run_with_context("任务 B", first.context)
        self.assertLess(
            second.context.review_memory_candidate.covered_through,
            second.context.latest_completed_task_seq,
        )
        business_calls = provider.calls
        original_messages = second.context.messages
        retry = _TaskSummarizer()
        agent.memory_summarizer = retry
        refresh = getattr(agent, "refresh_review_memory", None)
        self.assertIsNotNone(refresh, "CodingAgent 缺少显式记忆刷新入口")

        refreshed = refresh(second.context)

        self.assertEqual(business_calls, provider.calls)
        self.assertEqual(1, len(retry.calls))
        self.assertEqual(1, refreshed.memory_calls)
        self.assertEqual(original_messages, refreshed.context.messages)
        self.assertEqual(
            second.context.conversation_memory,
            refreshed.context.conversation_memory,
        )
        candidate = refreshed.context.review_memory_candidate
        self.assertEqual(
            refreshed.context.latest_completed_task_seq,
            candidate.covered_through,
        )
        self.assertEqual(2, len(candidate.constraints))

    def test_refresh_second_batch_failure_keeps_original_candidate_and_history(self) -> None:
        """两批刷新只有全部成功才发布，第二批失败不能留下第一批部分结果。"""

        provider = _FinishProvider()
        seed = _TaskSummarizer()
        seed_agent = _agent(provider, seed)
        context = SessionContext()
        for task in ("任务 A", "任务 B", "任务 C", "任务 D"):
            context = seed_agent.run_with_context(task, context).context
        backlog = replace(context, review_memory_candidate=None)
        failing = _TaskSummarizer(max_tasks=2, fail_call=2)
        agent = _agent(provider, failing)
        refresh = getattr(agent, "refresh_review_memory", None)
        self.assertIsNotNone(refresh, "CodingAgent 缺少显式记忆刷新入口")

        with self.assertRaises(MemorySummaryError):
            refresh(backlog)

        self.assertEqual(2, len(failing.calls))
        self.assertIsNone(backlog.review_memory_candidate)
        self.assertEqual(context.messages, backlog.messages)

    def test_refresh_reuses_complete_candidate_without_model_call(self) -> None:
        """候选已经覆盖最新任务时，刷新是无模型请求的幂等操作。"""

        provider = _FinishProvider()
        summarizer = _TaskSummarizer()
        agent = _agent(provider, summarizer)
        turn = agent.run_with_context("任务 A", SessionContext())
        calls_before = len(summarizer.calls)
        refresh = getattr(agent, "refresh_review_memory", None)
        self.assertIsNotNone(refresh, "CodingAgent 缺少显式记忆刷新入口")

        refreshed = refresh(turn.context)

        self.assertEqual(calls_before, len(summarizer.calls))
        self.assertEqual(0, refreshed.memory_calls)
        self.assertEqual(turn.context, refreshed.context)

    def test_refresh_is_rejected_when_persistence_is_off(self) -> None:
        """默认关闭模式不能因本地刷新命令额外调用模型。"""

        provider = _FinishProvider()
        summarizer = _TaskSummarizer()
        agent = _agent(provider, summarizer, persistence="off")
        refresh = getattr(agent, "refresh_review_memory", None)
        self.assertIsNotNone(refresh, "CodingAgent 缺少显式记忆刷新入口")

        with self.assertRaises(MemorySummaryError) as captured:
            refresh(SessionContext())
        self.assertEqual("disabled", captured.exception.code)
        self.assertEqual([], summarizer.calls)

    def test_runtime_refresh_publishes_only_the_complete_same_session_result(self) -> None:
        """Runtime 持锁提交完整快照，刷新后仍停留在原 Session。"""

        provider = _FinishProvider()
        initial = _TaskSummarizer(fail_call=2)
        agent = _agent(provider, initial)
        first = agent.run_with_context("任务 A", SessionContext())
        second = agent.run_with_context("任务 B", first.context)
        retry = _TaskSummarizer()
        agent.memory_summarizer = retry
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            seed_store = SessionStore(database, id_factory=lambda: "session-refresh")
            seed_store.initialize(workspace)
            seed_store.create("refresh", workspace, "openai", "test")
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
            runtime = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=lambda record, memory, options: ActiveSession(
                    record,
                    memory,
                    second.context,
                    config,
                    agent,
                ),
            )
            session_id = runtime.current.record.id

            refreshed = runtime.refresh_memory()

            self.assertEqual(session_id, runtime.current.record.id)
            self.assertEqual(refreshed.context, runtime.current.context)
            self.assertEqual(
                refreshed.context.latest_completed_task_seq,
                refreshed.context.review_memory_candidate.covered_through,
            )
            self.assertTrue(runtime._task_lock.acquire(blocking=False))
            runtime._task_lock.release()

    def test_refresh_rejects_candidate_behind_already_compacted_runtime_memory(self) -> None:
        """旧候选缺少已压缩区间时不得凭剩余消息伪造完整覆盖。"""

        provider = _FinishProvider()
        seed_summarizer = _TaskSummarizer()
        seed_agent = _agent(provider, seed_summarizer)
        first = seed_agent.run_with_context("任务 A", SessionContext()).context
        second = seed_agent.run_with_context("任务 B", first).context
        first_candidate = first.review_memory_candidate
        assert isinstance(first_candidate, ConversationMemory)
        second_messages = tuple(
            message
            for message in second.messages
            if message.task_id != first.messages[0].task_id
        )
        stale = ConversationMemory(
            revision=first_candidate.revision,
            generation=first_candidate.generation,
            covered_through=0,
        )
        compressed_gap = replace(
            second,
            messages=second_messages,
            conversation_memory=first_candidate,
            review_memory_candidate=stale,
        )
        retry = _TaskSummarizer()
        agent = _agent(provider, retry)

        with self.assertRaisesRegex(MemorySummaryError, "覆盖|恢复|压缩") as captured:
            agent.refresh_review_memory(compressed_gap)

        self.assertEqual("coverage", captured.exception.code)
        self.assertEqual([], retry.calls)
        self.assertEqual(stale, compressed_gap.review_memory_candidate)

    def test_runtime_cancellation_rejects_late_refresh_result(self) -> None:
        """摘要器忽略取消并迟到返回时，Runtime 仍不得提交候选。"""

        entered = threading.Event()
        release = threading.Event()
        original_context = SessionContext(
            review_memory_candidate=ConversationMemory(),
            next_message_seq=2,
            latest_completed_task_seq=1,
        )
        complete_context = replace(
            original_context,
            review_memory_candidate=ConversationMemory(
                revision=1,
                covered_through=1,
            ),
        )

        class LateRefreshAgent:
            def refresh_review_memory(self, context, cancellation):  # type: ignore[no-untyped-def]
                entered.set()
                release.wait(2)
                return MemoryRefreshResult(complete_context, memory_calls=1)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            store = SessionStore(database, id_factory=lambda: "session-cancel")
            store.initialize(workspace)
            store.create("cancel", workspace, "openai", "test")
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
            runtime = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=lambda record, memory, options: ActiveSession(
                    record,
                    memory,
                    original_context,
                    config,
                    LateRefreshAgent(),
                ),
            )
            errors: list[BaseException] = []

            def run_refresh() -> None:
                try:
                    runtime.refresh_memory()
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run_refresh)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(runtime.cancel_current())
            release.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], SessionRuntimeError)
            self.assertIn("取消", str(errors[0]))
            self.assertEqual(original_context, runtime.current.context)
            self.assertTrue(runtime._task_lock.acquire(blocking=False))
            runtime._task_lock.release()


if __name__ == "__main__":
    unittest.main()
