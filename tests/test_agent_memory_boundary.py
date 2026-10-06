from __future__ import annotations

import asyncio
import unittest
from dataclasses import FrozenInstanceError, replace

from tricoder.context.coordinator import (
    MemoryCoordinator,
    MemoryStepProgress,
)
from tricoder.context.manager import CompactionPlan, SaveCandidatePlan
from tricoder.context.memory import ConversationMemory, MemoryValidationError
from tricoder.context.summarizer import MemorySummaryError, MemorySummaryResult
from tricoder.core.cancellation import CancellationToken
from tricoder.engine.state import AgentRunState
from tricoder.engine.loop import AgentRunner
from tricoder.models import (
    MemoryConfig,
    MemoryRefreshResult,
    Message,
    SessionContext,
    TokenUsage,
)


class _Observer:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.errors: list[str] = []

    def on_error(self, message: str) -> None:
        self.errors.append(message)
        if self.failure is not None:
            raise self.failure


class _TwoBatchManager:
    """只控制提交时点；候选内容仍由可控摘要器产生。"""

    def plan_compaction(self, messages, *, covered_through=0, **_kwargs):  # type: ignore[no-untyped-def]
        copied = tuple(messages)
        if covered_through >= 2 or len(copied) < 2:
            return CompactionPlan(
                (), copied, covered_through, False, 10, 10, 3, 3, False
            )
        source = copied[:1]
        retained = copied[1:]
        covered = source[-1].message_seq or covered_through
        return CompactionPlan(
            source, retained, covered, True, 10, 5, 3, 2, False
        )

    @staticmethod
    def commit_compaction(context, plan, candidate, **_kwargs):  # type: ignore[no-untyped-def]
        return replace(
            context,
            messages=plan.retained_messages,
            conversation_memory=replace(
                candidate,
                revision=context.conversation_memory.revision + 1,
            ),
        )

    @staticmethod
    def plan_save_candidate(messages, *, covered_through):  # type: ignore[no-untyped-def]
        copied = tuple(messages)
        return SaveCandidatePlan(copied[:1], max(covered_through, 1), True)


class _CommitRejectingManager(_TwoBatchManager):
    """模拟终止待办触发容量或来源校验失败。"""

    @staticmethod
    def commit_compaction(context, plan, candidate, **_kwargs):  # type: ignore[no-untyped-def]
        raise MemoryValidationError("synthetic commit rejection")


class _TwoBatchSummarizer:
    def __init__(
        self,
        *,
        second_failure: BaseException | None = None,
    ) -> None:
        self.second_failure = second_failure
        self.calls = 0

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 2 and self.second_failure is not None:
            raise self.second_failure
        usage = TokenUsage(11, 1) if self.calls == 1 else TokenUsage(22, 2)
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=source[-1].message_seq or previous.covered_through,
            ),
            usage,
        )


def _state() -> AgentRunState:
    history = (
        Message("user", "旧任务 A", kind="task", message_seq=1, task_id="task-a"),
        Message("user", "旧任务 B", kind="task", message_seq=2, task_id="task-b"),
    )
    return AgentRunState.start(
        "系统提示",
        "当前任务",
        SessionContext(messages=history, next_message_seq=3),
    )


def _coordinator(
    summarizer: object,
    observer: _Observer,
    log,  # type: ignore[no-untyped-def]
) -> MemoryCoordinator:
    return MemoryCoordinator(
        _TwoBatchManager(),  # type: ignore[arg-type]
        MemoryConfig(compaction="structured", persistence="reviewed_summary"),
        summarizer,
        observer.on_error,
        log,
        audit_failure_message="audit failed",
    )


async def _prepare_compaction(
    coordinator: MemoryCoordinator,
    state: AgentRunState,
    cancellation: CancellationToken,
) -> None:
    progress = MemoryStepProgress()
    try:
        await coordinator.prepare_compaction(
            AgentRunner._memory_step_input(state, ()),
            cancellation,
            progress=progress,
        )
    finally:
        if progress.result is not None:
            AgentRunner._apply_memory_step(state, progress.result)


async def _prepare_review(
    coordinator: MemoryCoordinator,
    state: AgentRunState,
    cancellation: CancellationToken,
) -> None:
    progress = MemoryStepProgress()
    try:
        await coordinator.prepare_review_candidate(
            AgentRunner._memory_step_input(state, ()),
            cancellation,
            progress=progress,
        )
    finally:
        if progress.result is not None:
            AgentRunner._apply_memory_step(state, progress.result)


class AgentMemoryBoundaryCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_commit_rejection_becomes_safe_summary_failure(self) -> None:
        """提交校验失败必须保留原历史，并进入既有安全摘要失败路径。"""

        state = _state()
        original_messages = tuple(state.messages[state.history_start :])
        original_memory = state.conversation_memory
        coordinator = MemoryCoordinator(
            _CommitRejectingManager(),  # type: ignore[arg-type]
            MemoryConfig(compaction="structured"),
            _TwoBatchSummarizer(),
            lambda _message: None,
            lambda _event: True,
            audit_failure_message="audit failed",
        )

        with self.assertRaises(MemorySummaryError) as captured:
            await _prepare_compaction(coordinator, state, CancellationToken())

        self.assertEqual("commit", captured.exception.code)
        self.assertEqual(original_messages, tuple(state.messages[state.history_start :]))
        self.assertIs(original_memory, state.conversation_memory)
        self.assertEqual(1, state.memory_calls)
        self.assertEqual(TokenUsage(11, 1), state.memory_usage)

    async def test_coordinator_accepts_snapshot_without_engine_state(self) -> None:
        from tricoder.context import coordinator as module

        input_type = getattr(module, "MemoryStepInput", None)
        progress_type = getattr(module, "MemoryStepProgress", None)
        self.assertIsNotNone(input_type, "缺少快照输入类型")
        self.assertIsNotNone(progress_type, "缺少窄进度载体")
        if input_type is None or progress_type is None:
            return
        request = input_type(
            context=SessionContext(),
            fixed_messages=(Message("system", "fixed"),),
            provider_tools=(),
            summary_failed=False,
            warning="",
        )
        progress = progress_type()
        coordinator = MemoryCoordinator(
            _TwoBatchManager(),  # type: ignore[arg-type]
            MemoryConfig(),
            None,
            lambda _message: None,
            lambda _event: True,
            audit_failure_message="audit failed",
        )

        result = await coordinator.prepare_compaction(
            request,
            CancellationToken(),
            progress=progress,
        )

        self.assertIs(request.context, result.context)
        self.assertIs(result, progress.result)

    async def test_second_compaction_failure_keeps_first_commit_and_statistics(self) -> None:
        state = _state()
        summarizer = _TwoBatchSummarizer(
            second_failure=MemorySummaryError("第二批失败", code="provider")
        )
        observer = _Observer()
        coordinator = _coordinator(summarizer, observer, lambda _event: True)

        await _prepare_compaction(coordinator, state, CancellationToken())

        self.assertEqual(2, state.memory_calls)
        self.assertEqual(TokenUsage(11, 1), state.memory_usage)
        self.assertTrue(state.memory_summary_failed)
        self.assertTrue(state.memory_compacted)
        self.assertEqual(1, state.conversation_memory.covered_through)
        self.assertEqual(["旧任务 B", "用户任务：当前任务"], [m.content for m in state.messages[state.history_start :]])
        self.assertEqual(4, state.next_message_seq)
        self.assertEqual(1, len(observer.errors))

    async def test_second_compaction_audit_exception_keeps_usage_but_not_unreviewed_commit(self) -> None:
        state = _state()
        summarizer = _TwoBatchSummarizer()
        sentinel = RuntimeError("audit sentinel")
        compacted_events = 0

        def log(event):  # type: ignore[no-untyped-def]
            nonlocal compacted_events
            if event["status"] == "memory_compacted":
                compacted_events += 1
                if compacted_events == 2:
                    raise sentinel
            return True

        coordinator = _coordinator(summarizer, _Observer(), log)

        with self.assertRaises(RuntimeError) as captured:
            await _prepare_compaction(coordinator, state, CancellationToken())

        self.assertIs(sentinel, captured.exception)
        self.assertEqual(2, state.memory_calls)
        self.assertEqual(TokenUsage(33, 3), state.memory_usage)
        self.assertTrue(state.memory_compacted)
        self.assertEqual(1, state.conversation_memory.covered_through)
        self.assertEqual(["旧任务 B", "用户任务：当前任务"], [m.content for m in state.messages[state.history_start :]])

    async def test_native_cancellation_keeps_first_compaction_and_same_exception(self) -> None:
        state = _state()
        sentinel = asyncio.CancelledError("native cancellation")
        coordinator = _coordinator(
            _TwoBatchSummarizer(second_failure=sentinel),
            _Observer(),
            lambda _event: True,
        )

        try:
            await _prepare_compaction(coordinator, state, CancellationToken())
        except asyncio.CancelledError as captured:
            self.assertIs(sentinel, captured)
        else:  # pragma: no cover - 明确要求原生取消继续传播
            self.fail("原生取消被吞掉")

        self.assertEqual(2, state.memory_calls)
        self.assertEqual(TokenUsage(11, 1), state.memory_usage)
        self.assertFalse(state.memory_summary_failed)
        self.assertEqual(1, state.conversation_memory.covered_through)

    async def test_on_error_exception_keeps_failure_progress_and_identity(self) -> None:
        state = _state()
        sentinel = LookupError("observer sentinel")
        observer = _Observer(sentinel)
        coordinator = _coordinator(
            _TwoBatchSummarizer(
                second_failure=MemorySummaryError("第二批失败", code="provider")
            ),
            observer,
            lambda _event: True,
        )

        with self.assertRaises(LookupError) as captured:
            await _prepare_compaction(coordinator, state, CancellationToken())

        self.assertIs(sentinel, captured.exception)
        self.assertEqual(2, state.memory_calls)
        self.assertEqual(TokenUsage(11, 1), state.memory_usage)
        self.assertTrue(state.memory_summary_failed)
        self.assertEqual(1, state.conversation_memory.covered_through)

    async def test_review_audit_exception_keeps_original_candidate_and_old_statistics(self) -> None:
        state = _state()
        original = ConversationMemory(revision=3, covered_through=1)
        state.review_memory_candidate = original
        candidate = ConversationMemory(revision=3, covered_through=3)
        sentinel = RuntimeError("review audit sentinel")
        coordinator = _coordinator(_TwoBatchSummarizer(), _Observer(), lambda _event: (_ for _ in ()).throw(sentinel))

        async def build(_context, _cancellation):  # type: ignore[no-untyped-def]
            return MemoryRefreshResult(
                replace(_context, review_memory_candidate=candidate),
                memory_usage=TokenUsage(7, 5),
                memory_calls=2,
            )

        coordinator.build_review_candidate = build  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError) as captured:
            await _prepare_review(coordinator, state, CancellationToken())

        self.assertIs(sentinel, captured.exception)
        self.assertIs(original, state.review_memory_candidate)
        self.assertEqual(0, state.memory_calls)
        self.assertIsNone(state.memory_usage)

    async def test_review_failure_on_error_exception_keeps_warning_and_identity(self) -> None:
        state = _state()
        original = ConversationMemory(revision=3, covered_through=1)
        state.review_memory_candidate = original
        sentinel = ValueError("review observer sentinel")
        coordinator = _coordinator(
            _TwoBatchSummarizer(),
            _Observer(sentinel),
            lambda _event: True,
        )

        async def fail(_context, _cancellation):  # type: ignore[no-untyped-def]
            raise MemorySummaryError("两批构建失败", code="provider")

        coordinator.build_review_candidate = fail  # type: ignore[method-assign]

        with self.assertRaises(ValueError) as captured:
            await _prepare_review(coordinator, state, CancellationToken())

        self.assertIs(sentinel, captured.exception)
        self.assertIs(original, state.review_memory_candidate)
        self.assertIn("候选生成失败", state.memory_warning)
        self.assertEqual(0, state.memory_calls)
        self.assertIsNone(state.memory_usage)


class AgentMemoryBoundaryContractTests(unittest.TestCase):
    def test_runner_snapshot_keeps_every_fixed_prefix_message(self) -> None:
        state = AgentRunState.start(
            "system prompt",
            "task",
            SessionContext(
                persisted_summary="persisted",
                workspace_change_notice="workspace changed",
            ),
        )

        request = AgentRunner._memory_step_input(state, ())

        self.assertEqual(
            ("generic", "persisted_summary", "workspace_change"),
            tuple(message.kind for message in request.fixed_messages),
        )
        self.assertEqual(
            tuple(state.messages[state.history_start :]),
            request.context.messages,
        )

    def test_memory_step_types_are_narrow_and_immutable(self) -> None:
        from tricoder.context import coordinator as module

        input_type = getattr(module, "MemoryStepInput", None)
        result_type = getattr(module, "MemoryStepResult", None)
        progress_type = getattr(module, "MemoryStepProgress", None)
        self.assertIsNotNone(input_type, "缺少快照输入类型")
        self.assertIsNotNone(result_type, "缺少明确结果类型")
        self.assertIsNotNone(progress_type, "缺少每次调用独享的进度载体")
        if input_type is None or result_type is None or progress_type is None:
            return

        context = SessionContext()
        request = input_type(
            context=context,
            fixed_messages=(Message("system", "fixed"),),
            provider_tools=(),
            summary_failed=False,
            warning="",
        )
        result = result_type(
            context=context,
            memory_usage=None,
            memory_calls=0,
            summary_failed=False,
            compacted=False,
            warning="",
        )
        progress = progress_type(result)

        with self.assertRaises(FrozenInstanceError):
            request.warning = "changed"
        with self.assertRaises(FrozenInstanceError):
            result.memory_calls = 9
        self.assertIs(result, progress.result)
        self.assertNotIn("state", request.__dataclass_fields__)
        self.assertNotIn("state", result.__dataclass_fields__)

    def test_runner_merge_updates_only_memory_owned_fields(self) -> None:
        from tricoder.context import coordinator as module
        from tricoder.engine.loop import AgentRunner

        result_type = getattr(module, "MemoryStepResult", None)
        apply_step = getattr(AgentRunner, "_apply_memory_step", None)
        self.assertIsNotNone(result_type, "缺少明确结果类型")
        self.assertTrue(callable(apply_step), "Runner 缺少单点记忆合并")
        if result_type is None or not callable(apply_step):
            return

        state = _state()
        fixed_prefix = tuple(state.messages[: state.history_start])
        original_task_id = state.current_task_id
        evidence = object()
        state.modified_files = ["owned.py"]
        state.verification = "失败"
        state.evidence = evidence
        state.verification_required = True
        state.unknown_effects = True
        state.tool_calls = 7
        state.cleanup_failed = True
        state.file_effects_observed = False
        state.accumulated_usage = TokenUsage(101, 13)
        state.memory_usage = TokenUsage(2, 1)
        state.memory_calls = 4
        state.latest_completed_task_seq = 2
        memory = ConversationMemory(revision=8, covered_through=2)
        candidate = ConversationMemory(revision=8, covered_through=3)
        new_history = (
            Message(
                "user",
                "新历史",
                kind="task",
                message_seq=9,
                task_id="memory-result",
            ),
        )
        context = replace(
            state.execution_context(),
            messages=new_history,
            next_message_seq=10,
            conversation_memory=memory,
            review_memory_candidate=candidate,
            modified_files=("must-not-merge.py",),
            verification="通过",
            unknown_effects=False,
            verification_evidence=None,
            verification_required=False,
            latest_completed_task_seq=9,
        )
        result = result_type(
            context=context,
            memory_usage=TokenUsage(5, 3),
            memory_calls=2,
            summary_failed=True,
            compacted=True,
            warning="memory warning",
        )

        apply_step(state, result)

        self.assertEqual(fixed_prefix, tuple(state.messages[: state.history_start]))
        self.assertEqual(new_history, tuple(state.messages[state.history_start :]))
        self.assertEqual(10, state.next_message_seq)
        self.assertIs(memory, state.conversation_memory)
        self.assertIs(candidate, state.review_memory_candidate)
        self.assertEqual(TokenUsage(7, 4), state.memory_usage)
        self.assertEqual(6, state.memory_calls)
        self.assertTrue(state.memory_summary_failed)
        self.assertTrue(state.memory_compacted)
        self.assertEqual("memory warning", state.memory_warning)
        self.assertEqual(["owned.py"], state.modified_files)
        self.assertEqual("失败", state.verification)
        self.assertIs(evidence, state.evidence)
        self.assertTrue(state.verification_required)
        self.assertTrue(state.unknown_effects)
        self.assertEqual(7, state.tool_calls)
        self.assertTrue(state.cleanup_failed)
        self.assertFalse(state.file_effects_observed)
        self.assertEqual(TokenUsage(101, 13), state.accumulated_usage)
        self.assertEqual(2, state.latest_completed_task_seq)
        self.assertEqual(original_task_id, state.current_task_id)

    def test_separate_step_deltas_accumulate_once_and_none_usage_stays_missing(self) -> None:
        from tricoder.context.coordinator import MemoryStepResult

        state = _state()
        context = AgentRunner._memory_step_input(state, ()).context
        first = MemoryStepResult(
            context=context,
            memory_usage=TokenUsage(3, 1),
            memory_calls=1,
            summary_failed=False,
            compacted=False,
            warning="",
        )
        second = MemoryStepResult(
            context=context,
            memory_usage=None,
            memory_calls=2,
            summary_failed=False,
            compacted=False,
            warning="",
        )

        AgentRunner._apply_memory_step(state, first)
        AgentRunner._apply_memory_step(state, second)

        self.assertEqual(3, state.memory_calls)
        self.assertEqual(TokenUsage(3, 1), state.memory_usage)

        empty = _state()
        AgentRunner._apply_memory_step(empty, replace(first, memory_usage=None))
        self.assertIsNone(empty.memory_usage)

if __name__ == "__main__":
    unittest.main()
