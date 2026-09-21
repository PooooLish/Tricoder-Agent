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
    memory_to_json,
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


def _archived(index: int, *, text_size: int = 8) -> ArchivedMemoryItem:
    return ArchivedMemoryItem(
        "open_items",
        MemoryItem(
            f"done-{index}",
            f"已完成-{index}-" + ("x" * text_size),
            ("m1",),
            "session",
            state="done",
        ),
    )


class MemoryArchiveRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        store = SessionStore(self.database, id_factory=lambda: "session-archive")
        store.initialize(self.workspace)
        store.create("archive", self.workspace, "openai", "test")
        self.config = AppConfig(
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
                summary_max_chars=20_000,
            ),
        )
        self.runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=lambda record, memory, options: ActiveSession(
                record,
                memory,
                SessionContext(),
                self.config,
                object(),
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _set_candidate(self, candidate: ConversationMemory) -> None:
        self.runtime.current = replace(
            self.runtime.current,
            context=replace(
                self.runtime.current.context,
                conversation_memory=ConversationMemory(),
                review_memory_candidate=candidate,
                next_message_seq=candidate.covered_through + 1,
                latest_completed_task_seq=candidate.covered_through,
            ),
        )

    def test_archive_view_lists_metadata_and_both_capacity_limits(self) -> None:
        """列表只显示 ID/类别/状态及容量，不需要暴露归档正文。"""

        candidate = ConversationMemory(
            revision=2,
            covered_through=2,
            archived=(_archived(1),),
        )
        self._set_candidate(candidate)
        render = getattr(self.runtime, "render_memory_archive", None)
        self.assertIsNotNone(render, "Runtime 缺少归档查看入口")

        text = render()

        self.assertIn("done-1", text)
        self.assertIn("open_items", text)
        self.assertIn("done", text)
        self.assertIn("1/40", text)
        self.assertIn("字符容量", text)
        self.assertNotIn(candidate.archived[0].item.text, text)

    def test_confirmed_delete_targets_candidate_and_requires_save_for_restart(self) -> None:
        """确认只更新当前候选；再次保存后删除才跨重启生效。"""

        active = MemoryItem("constraint", "保留约束", ("m1",), "session")
        candidate = ConversationMemory(
            revision=2,
            covered_through=2,
            constraints=(active,),
            archived=(_archived(1),),
        )
        self._set_candidate(candidate)
        preview_method = getattr(self.runtime, "preview_memory_archive_delete", None)
        apply_method = getattr(self.runtime, "apply_memory_archive_delete", None)
        self.assertIsNotNone(preview_method, "Runtime 缺少归档删除预览入口")
        self.assertIsNotNone(apply_method, "Runtime 缺少归档删除确认入口")

        preview = preview_method("done-1")
        self.assertEqual(candidate, self.runtime.current.context.review_memory_candidate)
        self.assertIn("再次执行 /memory save", preview.text)
        trusted_before = replace(
            self.runtime.current.context,
            modified_files=("src/trusted.py",),
            verification="failed",
            unknown_effects=True,
        )
        self.runtime.current = replace(self.runtime.current, context=trusted_before)
        apply_method(preview)

        current = self.runtime.current.context
        edited = current.review_memory_candidate
        self.assertEqual((), edited.archived)
        self.assertEqual((active,), edited.constraints)
        self.assertEqual(3, edited.revision)
        self.assertEqual(3, current.next_message_seq)
        self.assertEqual(("src/trusted.py",), current.modified_files)
        self.assertEqual("failed", current.verification)
        self.assertTrue(current.unknown_effects)
        self.assertEqual(
            ConversationMemory(),
            self.runtime.current.context.conversation_memory,
        )
        self.assertIsNone(
            self.runtime.store.load_conversation_memory(
                self.runtime.current.record.id
            )
        )

        self.runtime.save_memory_preview(self.runtime.preview_memory_save())
        restarted = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=lambda record, memory, options: ActiveSession(
                record,
                memory,
                SessionContext(),
                self.config,
                object(),
            ),
        )
        self.assertEqual((), restarted.current.context.conversation_memory.archived)
        self.assertEqual((active,), restarted.current.context.conversation_memory.constraints)

    def test_stale_delete_preview_is_rejected_without_mutation(self) -> None:
        """候选变化后提交旧删除预览必须拒绝。"""

        candidate = ConversationMemory(
            revision=2,
            covered_through=2,
            archived=(_archived(1), _archived(2)),
        )
        self._set_candidate(candidate)
        preview_method = getattr(self.runtime, "preview_memory_archive_delete", None)
        apply_method = getattr(self.runtime, "apply_memory_archive_delete", None)
        self.assertIsNotNone(preview_method, "Runtime 缺少归档删除预览入口")
        self.assertIsNotNone(apply_method, "Runtime 缺少归档删除确认入口")
        preview = preview_method("done-1")
        changed = replace(candidate, revision=3, archived=(_archived(2),))
        self._set_candidate(changed)

        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            apply_method(preview)

        self.assertEqual(changed, self.runtime.current.context.review_memory_candidate)

    def test_reusing_confirmed_delete_preview_is_rejected(self) -> None:
        """同一确认预览只允许应用一次，不能重复推进 revision。"""

        candidate = ConversationMemory(
            revision=2,
            covered_through=2,
            archived=(_archived(1),),
        )
        self._set_candidate(candidate)
        preview = self.runtime.preview_memory_archive_delete("done-1")

        self.runtime.apply_memory_archive_delete(preview)
        after_first = self.runtime.current.context.review_memory_candidate
        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.apply_memory_archive_delete(preview)

        self.assertEqual(
            after_first,
            self.runtime.current.context.review_memory_candidate,
        )

    def test_delete_preview_cannot_cross_session_boundary(self) -> None:
        """A 会话的归档确认不能在切到 B 后删除任何内容。"""

        candidate = ConversationMemory(
            revision=2,
            covered_through=2,
            archived=(_archived(1),),
        )
        self._set_candidate(candidate)
        preview = self.runtime.preview_memory_archive_delete("done-1")

        other = self.runtime.create("other")
        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.apply_memory_archive_delete(preview)

        self.assertEqual(other.record.id, self.runtime.current.record.id)
        self.assertEqual(
            ConversationMemory(),
            self.runtime.current.context.conversation_memory,
        )
        self.assertIsNone(self.runtime.current.context.review_memory_candidate)

    def test_deleting_one_of_forty_archives_releases_capacity(self) -> None:
        """归档满 40 条时，确认删除一条后可再次归档终结项。"""

        pending = MemoryItem(
            "todo",
            "新待办",
            ("m1",),
            "session",
            state="pending",
        )
        full = ConversationMemory(
            revision=2,
            covered_through=2,
            open_items=(pending,),
            archived=tuple(_archived(index) for index in range(40)),
        )
        self._set_candidate(full)
        preview_method = getattr(self.runtime, "preview_memory_archive_delete", None)
        apply_method = getattr(self.runtime, "apply_memory_archive_delete", None)
        self.assertIsNotNone(preview_method, "Runtime 缺少归档删除预览入口")
        self.assertIsNotNone(apply_method, "Runtime 缺少归档删除确认入口")
        apply_method(preview_method("done-0"))
        cleaned = self.runtime.current.context.review_memory_candidate

        terminal = merge_review_candidate(
            cleaned,
            ConversationMemory(
                revision=cleaned.revision,
                covered_through=3,
                open_items=(replace(pending, source_ids=("m3",), state="done"),),
            ),
            allowed_source_ids={"m3"},
            max_chars=20_000,
        )

        self.assertEqual(40, len(terminal.archived))
        self.assertNotIn("done-0", {entry.item.id for entry in terminal.archived})
        self.assertIn("todo", {entry.item.id for entry in terminal.archived})

    def test_deleting_archive_releases_character_capacity(self) -> None:
        """未满 40 条时也能通过删除大归档释放字符容量。"""

        large_archive = _archived(1, text_size=430)
        full = ConversationMemory(
            revision=2,
            covered_through=2,
            archived=(large_archive, _archived(2, text_size=430)),
        )
        proposed = MemoryItem(
            "constraint-new",
            "必须保留新的接口契约" + ("y" * 400),
            ("m3",),
            "session",
        )
        unrestricted = merge_review_candidate(
            full,
            ConversationMemory(
                revision=full.revision,
                covered_through=3,
                constraints=(proposed,),
            ),
            allowed_source_ids={"m3"},
            max_chars=20_000,
        )
        cleaned = replace(
            full,
            revision=full.revision + 1,
            archived=(full.archived[1],),
        )
        cleaned_merged = merge_review_candidate(
            cleaned,
            ConversationMemory(
                revision=cleaned.revision,
                covered_through=3,
                constraints=(proposed,),
            ),
            allowed_source_ids={"m3"},
            max_chars=20_000,
        )
        limit = max(len(memory_to_json(full)), len(memory_to_json(cleaned_merged))) + 1
        self.assertLess(limit, len(memory_to_json(unrestricted)))
        limited_config = replace(
            self.config,
            memory=replace(self.config.memory, summary_max_chars=limit),
        )
        self.runtime.current = replace(self.runtime.current, config=limited_config)
        self._set_candidate(full)

        with self.assertRaises(MemoryValidationError):
            merge_review_candidate(
                full,
                ConversationMemory(
                    revision=full.revision,
                    covered_through=3,
                    constraints=(proposed,),
                ),
                allowed_source_ids={"m3"},
                max_chars=limit,
            )

        self.runtime.apply_memory_archive_delete(
            self.runtime.preview_memory_archive_delete("done-1")
        )
        after_delete = self.runtime.current.context.review_memory_candidate
        merged = merge_review_candidate(
            after_delete,
            ConversationMemory(
                revision=after_delete.revision,
                covered_through=3,
                constraints=(proposed,),
            ),
            allowed_source_ids={"m3"},
            max_chars=limit,
        )

        self.assertEqual((proposed,), merged.constraints)
        self.assertEqual((full.archived[1],), merged.archived)


if __name__ == "__main__":
    unittest.main()
