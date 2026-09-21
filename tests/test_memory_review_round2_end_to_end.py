from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tricoder.context.memory import (
    ArchivedMemoryItem,
    ConversationMemory,
    MemoryItem,
    MemoryValidationError,
    merge_review_candidate,
)
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, SessionContext
from tricoder.session_runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.sessions import SessionStore
from tests.test_memory_refresh import _FinishProvider, _TaskSummarizer, _agent


def _candidate_update(
    previous: ConversationMemory,
    *,
    covered_through: int,
    decisions: tuple[MemoryItem, ...] = (),
    open_items: tuple[MemoryItem, ...] = (),
    allowed_source_ids: set[str],
) -> ConversationMemory:
    """构造一轮确定性摘要更新，仍走生产合并与严格校验。"""

    return merge_review_candidate(
        previous,
        ConversationMemory(
            revision=previous.revision,
            generation=previous.generation,
            covered_through=covered_through,
            decisions=decisions,
            open_items=open_items,
        ),
        allowed_source_ids=allowed_source_ids,
        max_chars=20_000,
    )


class MemoryReviewRound2EndToEndTests(unittest.TestCase):
    def test_stale_refresh_replace_archive_delete_save_restart_flow(self) -> None:
        """连续覆盖 S1、S2、S3，并核对最终 SQLite 恢复状态。"""

        provider = _FinishProvider()
        initial_summarizer = _TaskSummarizer(fail_call=2)
        agent = _agent(provider, initial_summarizer)
        first = agent.run_with_context("任务 A", SessionContext())
        second = agent.run_with_context("任务 B", first.context)
        stale = second.context.review_memory_candidate
        assert isinstance(stale, ConversationMemory)
        self.assertLess(stale.covered_through, second.context.latest_completed_task_seq)
        business_calls = provider.calls
        original_messages = second.context.messages

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            seed_store = SessionStore(database, id_factory=lambda: "session-round2")
            seed_store.initialize(workspace)
            seed_store.create("round2", workspace, "openai", "test")
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
                    summary_max_chars=20_000,
                ),
            )
            retry_summarizer = _TaskSummarizer()
            agent.memory_summarizer = retry_summarizer
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

            with self.assertRaisesRegex(SessionRuntimeError, "覆盖|刷新"):
                runtime.preview_memory_save()
            self.assertIsNone(
                runtime.store.load_conversation_memory(runtime.current.record.id)
            )

            refreshed = runtime.refresh_memory()
            refreshed_candidate = refreshed.context.review_memory_candidate
            assert isinstance(refreshed_candidate, ConversationMemory)
            self.assertEqual(business_calls, provider.calls)
            self.assertEqual(1, refreshed.memory_calls)
            self.assertEqual(original_messages, refreshed.context.messages)
            self.assertEqual(
                refreshed.context.latest_completed_task_seq,
                refreshed_candidate.covered_through,
            )
            runtime.save_memory_preview(runtime.preview_memory_save())
            first_saved_revision = runtime.current.context.persisted_memory_revision
            self.assertIsNotNone(first_saved_revision)

            covered = refreshed_candidate.covered_through
            old = MemoryItem(
                "decision-old",
                "使用旧方案",
                (f"m{covered + 1}",),
                "session",
            )
            with_old = _candidate_update(
                refreshed_candidate,
                covered_through=covered + 1,
                decisions=(old,),
                allowed_source_ids={f"m{covered + 1}"},
            )
            replacement = MemoryItem(
                "decision-new",
                "使用新方案",
                (f"m{covered + 2}",),
                "session",
                replaces_id="decision-old",
            )
            replaced = _candidate_update(
                with_old,
                covered_through=covered + 2,
                decisions=(replacement,),
                allowed_source_ids={f"m{covered + 2}"},
            )
            repeated = _candidate_update(
                replaced,
                covered_through=covered + 3,
                decisions=(replacement,),
                allowed_source_ids=set(),
            )
            self.assertEqual((replacement,), repeated.decisions)
            self.assertEqual(replaced.archived, repeated.archived)

            pending = MemoryItem(
                "cleanup-todo",
                "完成归档清理",
                (f"m{covered + 3}",),
                "session",
                state="pending",
            )
            generated_archives = tuple(
                ArchivedMemoryItem(
                    "open_items",
                    MemoryItem(
                        f"done-{index}",
                        f"历史事项 {index}",
                        ("m1",),
                        "session",
                        state="done",
                    ),
                )
                for index in range(39)
            )
            full = replace(
                repeated,
                revision=repeated.revision + 1,
                open_items=(pending,),
                archived=repeated.archived + generated_archives,
            )
            self.assertEqual(40, len(full.archived))
            terminal = replace(
                pending,
                source_ids=(f"m{covered + 4}",),
                state="done",
            )
            with self.assertRaises(MemoryValidationError):
                _candidate_update(
                    full,
                    covered_through=covered + 4,
                    open_items=(terminal,),
                    allowed_source_ids={f"m{covered + 4}"},
                )

            runtime.current = replace(
                runtime.current,
                context=replace(
                    runtime.current.context,
                    review_memory_candidate=full,
                    next_message_seq=covered + 5,
                    latest_completed_task_seq=covered + 4,
                ),
            )
            delete_preview = runtime.preview_memory_archive_delete("done-0")
            self.assertEqual(40, len(full.archived))
            runtime.apply_memory_archive_delete(delete_preview)
            cleaned = runtime.current.context.review_memory_candidate
            assert isinstance(cleaned, ConversationMemory)
            final_candidate = _candidate_update(
                cleaned,
                covered_through=covered + 4,
                open_items=(terminal,),
                allowed_source_ids={f"m{covered + 4}"},
            )
            runtime.current = replace(
                runtime.current,
                context=replace(
                    runtime.current.context,
                    review_memory_candidate=final_candidate,
                ),
            )
            runtime.save_memory_preview(runtime.preview_memory_save())
            self.assertGreater(
                runtime.current.context.persisted_memory_revision,
                first_saved_revision,
            )

            restarted = SessionRuntime(
                SessionStore(database),
                workspace,
                options=RuntimeOptions(),
                active_session_factory=lambda record, memory, options: ActiveSession(
                    record,
                    memory,
                    SessionContext(),
                    config,
                    object(),
                ),
            )
            restored = restarted.current.context.conversation_memory
            self.assertEqual(final_candidate, restored)
            self.assertEqual((replacement,), restored.decisions)
            self.assertEqual(40, len(restored.archived))
            self.assertNotIn("done-0", {entry.item.id for entry in restored.archived})
            self.assertIn(
                "cleanup-todo",
                {entry.item.id for entry in restored.archived},
            )
            self.assertEqual(covered + 4, restored.covered_through)


if __name__ == "__main__":
    unittest.main()
