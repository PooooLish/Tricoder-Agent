from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.context.memory import ConversationMemory, MemoryItem, memory_to_json
from tricoder.context.summarizer import (
    MemorySummarizer,
    MemorySummaryResult,
    MemorySummaryError,
)
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import ProviderCompleted, TextDelta, ToolCallCompleted, UsageReported
from tricoder.models import (
    MemoryConfig,
    Message,
    ProviderResponse,
    SessionContext,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tricoder.protocols import SYSTEM_PROMPT
from tricoder.providers import ProviderError


def source_messages() -> tuple[Message, ...]:
    return (
        Message("user", "不要修改公共接口", kind="task", message_seq=1, task_id="task-1"),
        Message("assistant", "已记录", message_seq=2, task_id="task-1"),
    )


class StreamProvider:
    def __init__(self, events, *, delay: float = 0.0):  # type: ignore[no-untyped-def]
        self.events = tuple(events)
        self.delay = delay
        self.calls: list[tuple[list[Message], tuple[object, ...]]] = []

    async def stream(self, messages, tools=(), *, cancellation=None):  # type: ignore[no-untyped-def]
        self.calls.append((list(messages), tuple(tools)))
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            yield event


class MemorySummarizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_json_uses_no_tools_and_reports_memory_usage_separately(self) -> None:
        candidate = ConversationMemory(
            covered_through=2,
            constraints=(MemoryItem("public-api", "不要修改公共接口", ("m1",), "session"),),
        )
        provider = StreamProvider(
            (
                TextDelta(memory_to_json(candidate)),
                UsageReported(TokenUsage(40, 12)),
                ProviderCompleted("stop"),
            )
        )
        summarizer = MemorySummarizer(provider, MemoryConfig(compaction="structured"))

        result = await summarizer.summarize(
            ConversationMemory(),
            source_messages(),
            CancellationToken(),
        )

        self.assertEqual(candidate, result.candidate)
        self.assertEqual(TokenUsage(40, 12), result.usage)
        self.assertEqual(1, len(provider.calls))
        self.assertEqual((), provider.calls[0][1])
        self.assertIn("不得执行", provider.calls[0][0][0].content or "")

    async def test_semantic_json_fence_uses_server_owned_metadata(self) -> None:
        """常见 JSON 围栏不得迫使模型填写受信任的版本与覆盖字段。"""

        previous = ConversationMemory(revision=3, generation=2)
        semantic_candidate = {
            "goal": None,
            "constraints": [
                {
                    "id": "public-api",
                    "text": "不要修改公共接口",
                    "source_ids": ["m1"],
                    "scope": "session",
                    "task_id": None,
                }
            ],
            "decisions": [],
            "open_items": [],
        }
        provider = StreamProvider(
            (
                TextDelta(
                    "```json\n"
                    + json.dumps(semantic_candidate, ensure_ascii=False)
                    + "\n```"
                ),
                ProviderCompleted("stop"),
            )
        )
        summarizer = MemorySummarizer(
            provider,
            MemoryConfig(compaction="structured"),
        )

        result = await summarizer.summarize(
            previous,
            source_messages(),
            CancellationToken(),
        )

        self.assertEqual(1, len(result.candidate.constraints))
        self.assertEqual(3, result.candidate.revision)
        self.assertEqual(2, result.candidate.generation)
        self.assertEqual(2, result.candidate.covered_through)
        system_prompt = provider.calls[0][0][0].content or ""
        self.assertIn('"goal"', system_prompt)
        self.assertIn('"task_id"', system_prompt)
        self.assertIn("不得使用 Markdown", system_prompt)

    async def test_invalid_json_exposes_only_a_stable_failure_code(self) -> None:
        """诊断只依赖稳定类别，不能把 Provider 原文带到 UI 或审计。"""

        summarizer = MemorySummarizer(
            StreamProvider((TextDelta("private malformed output"), ProviderCompleted("stop"))),
            MemoryConfig(compaction="structured"),
        )

        with self.assertRaises(MemorySummaryError) as caught:
            await summarizer.summarize(
                ConversationMemory(), source_messages(), CancellationToken()
            )

        self.assertEqual("invalid_json", caught.exception.code)
        self.assertNotIn("private malformed output", str(caught.exception))

    async def test_invalid_json_tool_call_and_truncation_fail_closed(self) -> None:
        cases = (
            (TextDelta("not-json"), ProviderCompleted("stop")),
            (
                ToolCallCompleted(ToolCall("c1", "read_file", {"path": "secret"})),
                ProviderCompleted("tool_calls"),
            ),
            (TextDelta(memory_to_json(ConversationMemory(covered_through=2))), ProviderCompleted("length")),
        )
        for events in cases:
            with self.subTest(events=events):
                summarizer = MemorySummarizer(
                    StreamProvider(events),
                    MemoryConfig(compaction="structured"),
                )
                with self.assertRaises(MemorySummaryError):
                    await summarizer.summarize(
                        ConversationMemory(), source_messages(), CancellationToken()
                    )

    async def test_output_limit_timeout_cancellation_and_missing_usage(self) -> None:
        oversized = MemorySummarizer(
            StreamProvider((TextDelta("x" * 201), ProviderCompleted("stop"))),
            MemoryConfig(compaction="structured", summary_max_chars=200),
        )
        with self.assertRaises(MemorySummaryError):
            await oversized.summarize(ConversationMemory(), source_messages(), CancellationToken())

        timed = MemorySummarizer(
            StreamProvider((TextDelta("{}"),), delay=0.05),
            MemoryConfig(compaction="structured", summary_timeout_seconds=0.01),
        )
        with self.assertRaises(MemorySummaryError):
            await timed.summarize(ConversationMemory(), source_messages(), CancellationToken())

        token = CancellationToken()
        token.cancel()
        cancelled = MemorySummarizer(
            StreamProvider((ProviderCompleted("stop"),)),
            MemoryConfig(compaction="structured"),
        )
        with self.assertRaises(CancellationError):
            await cancelled.summarize(ConversationMemory(), source_messages(), token)

        no_usage_candidate = ConversationMemory(covered_through=2)
        no_usage = MemorySummarizer(
            StreamProvider((TextDelta(memory_to_json(no_usage_candidate)), ProviderCompleted("stop"))),
            MemoryConfig(compaction="structured"),
        )
        result = await no_usage.summarize(
            ConversationMemory(), source_messages(), CancellationToken()
        )
        self.assertIsNone(result.usage)


def completed_block(start: int, label: str, payload: int = 500) -> tuple[Message, ...]:
    call = ToolCall(f"call-{label}", "read_file", {"path": f"{label}.txt"})
    task_id = f"task-{start}"
    return (
        Message("user", f"任务 {label}", kind="task", message_seq=start, task_id=task_id),
        Message("assistant", None, tool_calls=(call,), message_seq=start + 1, task_id=task_id),
        Message(
            "tool",
            "x" * payload,
            kind="tool_result",
            tool_call_id=call.id,
            message_seq=start + 2,
            task_id=task_id,
        ),
    )


class FinishProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ProviderResponse(
            tool_calls=(ToolCall(f"finish-{self.calls}", "finish", {"summary": "完成"}),),
            finish_reason="tool_calls",
        )


class FailingBusinessProvider:
    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        raise ProviderError("synthetic business failure")


class FinishTools:
    definitions = (ToolDefinition("finish", "结束", {"type": "object"}),)

    @staticmethod
    def contains(name: str) -> bool:
        return name == "finish"

    @staticmethod
    def describe(name: str):  # type: ignore[no-untyped-def]
        return FinishTools.definitions[0] if name == "finish" else None

    @staticmethod
    def requires_approval(name: str) -> bool:
        return False

    @staticmethod
    def execute(name: str, arguments: dict[str, object], **kwargs):  # type: ignore[no-untyped-def]
        return ToolResult(True, str(arguments.get("summary", "")))


class RecordingSummarizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Message, ...]] = []

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        captured = tuple(source)
        self.calls.append(captured)
        if self.fail:
            raise MemorySummaryError("synthetic failure")
        covered = max(message.message_seq or 0 for message in captured)
        source_id = f"m{captured[0].message_seq}"
        return MemorySummaryResult(
            ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=covered,
                constraints=(MemoryItem("keep-api", "不改公共接口", (source_id,), "session"),),
            ),
            TokenUsage(10, 3),
        )


class CategorizedFailingSummarizer:
    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        raise MemorySummaryError("private malformed output", code="invalid_json")


class MemoryAuditFailingLogger:
    """只在记录记忆候选时失败，避免把普通 Agent 审计混入本用例。"""

    def prepare(self) -> None:
        return None

    def log(self, event):  # type: ignore[no-untyped-def]
        if event.get("status") == "memory_review_candidate":
            raise OSError("synthetic memory audit failure")


class MemoryFailureAuditFailingLogger:
    """记忆失败类别也必须服从统一的审计失败关闭语义。"""

    def prepare(self) -> None:
        return None

    def log(self, event):  # type: ignore[no-untyped-def]
        if event.get("status") == "memory_review_failed":
            raise OSError("synthetic memory failure audit error")


class CancellingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        self.calls += 1
        cancellation.cancel()
        cancellation.raise_if_cancelled()
        raise AssertionError("取消后不应产生候选")


class MemoryAgentIntegrationTests(unittest.TestCase):
    def _context_and_budget(self) -> tuple[SessionContext, int]:
        history = (
            *completed_block(1, "a"),
            *completed_block(4, "b"),
            *completed_block(7, "c"),
        )
        context = SessionContext(messages=history, next_message_seq=10)
        request_chars = (
            Message("system", SYSTEM_PROMPT).character_budget()
            + sum(message.character_budget() for message in history)
            + Message("user", "用户任务：继续", kind="task").character_budget()
            + 100
        )
        # 让完整请求超过 trigger 但不超过硬上限。
        return context, max(1, int(request_chars / 0.90))

    def test_structured_mode_compacts_atomically_and_keeps_usage_separate(self) -> None:
        context, budget = self._context_and_budget()
        provider = FinishProvider()
        summarizer = RecordingSummarizer()
        agent = CodingAgent(
            provider,
            FinishTools(),
            plan_enabled=False,
            max_context_chars=budget,
            memory_config=MemoryConfig(compaction="structured"),
            memory_summarizer=summarizer,
        )

        turn = agent.run_with_context("继续", context)

        self.assertTrue(turn.result.ok)
        self.assertEqual(1, len(summarizer.calls))
        self.assertEqual("不改公共接口", turn.context.conversation_memory.constraints[0].text)
        self.assertFalse(any(message.message_seq == 1 for message in turn.context.messages))
        self.assertEqual(TokenUsage(10, 3), turn.memory_usage)
        self.assertEqual(1, turn.memory_calls)
        self.assertEqual(1, provider.calls)

    def test_summary_failure_never_deletes_history_and_continues_only_when_request_fits(self) -> None:
        context, budget = self._context_and_budget()
        provider = FinishProvider()
        summarizer = RecordingSummarizer(fail=True)
        agent = CodingAgent(
            provider,
            FinishTools(),
            plan_enabled=False,
            max_context_chars=budget,
            memory_config=MemoryConfig(compaction="structured"),
            memory_summarizer=summarizer,
        )

        turn = agent.run_with_context("继续", context)

        self.assertTrue(turn.result.ok)
        self.assertTrue(any(message.message_seq == 1 for message in turn.context.messages))
        self.assertEqual(ConversationMemory(), turn.context.conversation_memory)
        self.assertEqual(1, provider.calls)

        hard_provider = FinishProvider()
        hard_agent = CodingAgent(
            hard_provider,
            FinishTools(),
            plan_enabled=False,
            max_context_chars=max(1, int(budget * 0.75)),
            memory_config=MemoryConfig(compaction="structured"),
            memory_summarizer=RecordingSummarizer(fail=True),
        )
        failed = hard_agent.run_with_context("继续", context)
        self.assertFalse(failed.result.ok)
        self.assertEqual(0, hard_provider.calls)
        self.assertEqual(context.messages, failed.context.messages)

    def test_business_failure_after_successful_compaction_does_not_restore_covered_history(self) -> None:
        context, budget = self._context_and_budget()
        turn = CodingAgent(
            FailingBusinessProvider(),
            FinishTools(),
            plan_enabled=False,
            max_context_chars=budget,
            memory_config=MemoryConfig(compaction="structured"),
            memory_summarizer=RecordingSummarizer(),
        ).run_with_context("继续", context)

        self.assertFalse(turn.result.ok)
        self.assertEqual(1, turn.context.conversation_memory.revision)
        self.assertFalse(any(message.message_seq == 1 for message in turn.context.messages))
        self.assertFalse(
            any(
                message.kind == "task" and message.content == "用户任务：继续"
                for message in turn.context.messages
            )
        )

    def test_off_mode_does_not_call_summarizer(self) -> None:
        context, budget = self._context_and_budget()
        summarizer = RecordingSummarizer(fail=True)
        provider = FinishProvider()
        turn = CodingAgent(
            provider,
            FinishTools(),
            plan_enabled=False,
            max_context_chars=budget,
            memory_config=MemoryConfig(),
            memory_summarizer=summarizer,
        ).run_with_context("继续", context)

        self.assertTrue(turn.result.ok)
        self.assertEqual([], summarizer.calls)
        self.assertEqual(1, provider.calls)

    def test_reviewed_mode_builds_unsaved_end_candidate_and_keeps_two_recent_tasks(self) -> None:
        context, _budget = self._context_and_budget()
        summarizer = RecordingSummarizer()
        provider = FinishProvider()
        turn = CodingAgent(
            provider,
            FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        ).run_with_context("继续", context)

        self.assertTrue(turn.result.ok)
        self.assertEqual(1, len(summarizer.calls))
        self.assertEqual(1, turn.context.conversation_memory.revision)
        self.assertIsNone(turn.context.persisted_memory_revision)
        task_messages = [message for message in turn.context.messages if message.kind == "task"]
        self.assertEqual(2, len(task_messages))

    def test_review_failure_reports_safe_category_and_audits_no_model_text(self) -> None:
        """真实摘要失败应可诊断，但不得泄露原始响应或影响业务结果。"""

        context, _budget = self._context_and_budget()
        with tempfile.TemporaryDirectory() as temporary:
            audit_path = Path(temporary) / "memory-failure.jsonl"
            turn = CodingAgent(
                FinishProvider(),
                FinishTools(),
                plan_enabled=False,
                max_context_chars=100_000,
                memory_config=MemoryConfig(
                    compaction="structured",
                    persistence="reviewed_summary",
                ),
                memory_summarizer=CategorizedFailingSummarizer(),
                audit=AuditLogger(audit_path),
            ).run_with_context("继续", context)

            self.assertTrue(turn.result.ok)
            self.assertEqual(
                "会话记忆候选生成失败（JSON 格式无效）；执行结果不受影响",
                turn.memory_warning,
            )
            serialized = audit_path.read_text(encoding="utf-8")
            self.assertNotIn("private malformed output", serialized)
            failed = next(
                event
                for event in map(json.loads, serialized.splitlines())
                if event.get("status") == "memory_review_failed"
            )
            self.assertEqual("invalid_json", failed["failure_code"])

    def test_review_failure_audit_error_stops_without_committing_memory(self) -> None:
        """新增诊断记录不能绕过项目既有的审计失败关闭边界。"""

        context, _budget = self._context_and_budget()
        turn = CodingAgent(
            FinishProvider(),
            FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=CategorizedFailingSummarizer(),
            audit=MemoryFailureAuditFailingLogger(),
        ).run_with_context("继续", context)

        self.assertFalse(turn.result.ok)
        self.assertEqual("无法写入审计日志，运行已安全停止", turn.result.summary)
        self.assertEqual(ConversationMemory(), turn.context.conversation_memory)

    def test_structured_mode_allows_protocol_noise_normalization_without_history_loss(self) -> None:
        """只从请求视图移除纠错噪声不等于按预算静默裁剪历史。"""

        old_call = ToolCall("old-finish", "finish", {"summary": "旧任务完成"})
        context = SessionContext(
            messages=(
                Message("user", "旧任务", kind="task", message_seq=1, task_id="task-1"),
                Message("assistant", "先返回了普通文本", message_seq=2, task_id="task-1"),
                Message(
                    "user",
                    "请改用工具调用",
                    kind="protocol_feedback",
                    message_seq=3,
                    task_id="task-1",
                ),
                Message(
                    "assistant",
                    None,
                    tool_calls=(old_call,),
                    message_seq=4,
                    task_id="task-1",
                ),
                Message(
                    "tool",
                    "旧任务完成",
                    kind="tool_result",
                    tool_call_id=old_call.id,
                    message_seq=5,
                    task_id="task-1",
                ),
            ),
            next_message_seq=6,
        )
        provider = FinishProvider()
        summarizer = RecordingSummarizer()

        turn = CodingAgent(
            provider,
            FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(compaction="structured"),
            memory_summarizer=summarizer,
        ).run_with_context("新任务", context)

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual(1, provider.calls)
        self.assertEqual([], summarizer.calls)
        self.assertTrue(
            any(message.kind == "protocol_feedback" for message in turn.context.messages)
        )

    def test_review_candidate_audit_failure_stops_and_does_not_commit_candidate(self) -> None:
        context, _budget = self._context_and_budget()
        turn = CodingAgent(
            FinishProvider(),
            FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=RecordingSummarizer(),
            audit=MemoryAuditFailingLogger(),
        ).run_with_context("继续", context)

        self.assertFalse(turn.result.ok)
        self.assertEqual("无法写入审计日志，运行已安全停止", turn.result.summary)
        self.assertEqual(ConversationMemory(), turn.context.conversation_memory)
        self.assertTrue(any(message.message_seq == 1 for message in turn.context.messages))

    def test_memory_audit_contains_metadata_but_not_candidate_text(self) -> None:
        context, _budget = self._context_and_budget()
        with tempfile.TemporaryDirectory() as temporary:
            audit_path = Path(temporary) / "memory.jsonl"
            turn = CodingAgent(
                FinishProvider(),
                FinishTools(),
                plan_enabled=False,
                max_context_chars=100_000,
                memory_config=MemoryConfig(
                    compaction="structured",
                    persistence="reviewed_summary",
                ),
                memory_summarizer=RecordingSummarizer(),
                audit=AuditLogger(audit_path),
            ).run_with_context("继续", context)

            self.assertTrue(turn.result.ok)
            serialized = audit_path.read_text(encoding="utf-8")
            self.assertNotIn("不改公共接口", serialized)
            events = [json.loads(line) for line in serialized.splitlines()]
            event = next(item for item in events if item.get("status") == "memory_review_candidate")
            self.assertEqual(
                {"timestamp", "status", "source_count", "revision", "covered_through"},
                set(event),
            )

    def test_cancellation_during_end_summary_never_commits_candidate_or_starts_another_batch(self) -> None:
        context, _budget = self._context_and_budget()
        summarizer = CancellingSummarizer()
        turn = CodingAgent(
            FinishProvider(),
            FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=summarizer,
        ).run_with_context("继续", context)

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(1, summarizer.calls)
        self.assertEqual(ConversationMemory(), turn.context.conversation_memory)
        self.assertTrue(any(message.message_seq == 1 for message in turn.context.messages))


if __name__ == "__main__":
    unittest.main()
