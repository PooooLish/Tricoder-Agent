from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tricoder.context.memory import (
    ConversationMemory,
    MemoryItem,
    memory_to_json,
    merge_review_candidate,
)
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    SessionContext,
    SessionMemory,
)
from tricoder.session_runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.sessions import SessionError, SessionStore


class MemoryPersistenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initialize_upgrades_synthetic_old_database_without_changing_old_rows(self) -> None:
        self.database.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE session_memory (
                    session_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
                    requirements_summary TEXT NOT NULL DEFAULT '',
                    last_task_summary TEXT NOT NULL DEFAULT '',
                    modified_files_json TEXT NOT NULL DEFAULT '[]',
                    verification TEXT NOT NULL DEFAULT '未运行',
                    permission TEXT NOT NULL DEFAULT 'strict',
                    unknown_effects INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("old", "old", str(self.workspace), "openai", "test", "t0", "t0"),
            )
            connection.execute(
                "INSERT INTO session_memory(session_id, summary) VALUES (?, ?)",
                ("old", "legacy-summary"),
            )
            connection.commit()
        finally:
            connection.close()

        store = SessionStore(self.database)
        store.initialize(self.workspace)

        self.assertEqual("legacy-summary", store.load_memory("old").summary)
        self.assertIsNone(store.load_conversation_memory("old"))
        connection = sqlite3.connect(self.database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
        self.assertIn("conversation_memory", tables)

    def test_save_load_restart_and_revision_compare_are_transactional(self) -> None:
        store = SessionStore(self.database, id_factory=lambda: "session-1")
        store.initialize(self.workspace)
        record = store.create("one", self.workspace, "openai", "test")
        first = ConversationMemory(
            revision=1,
            covered_through=2,
            constraints=(MemoryItem("api", "不改公共接口", ("m1",), "session"),),
        )

        saved_revision = store.save_conversation_memory(
            record.id, first, next_message_seq=3, expected_revision=None
        )
        restarted = SessionStore(self.database)
        restarted.initialize(self.workspace)

        self.assertEqual(1, saved_revision)
        self.assertEqual((first, 3), restarted.load_conversation_memory(record.id))
        with self.assertRaises(SessionError):
            restarted.save_conversation_memory(
                record.id,
                ConversationMemory(revision=2, covered_through=2),
                next_message_seq=3,
                expected_revision=None,
            )
        self.assertEqual((first, 3), restarted.load_conversation_memory(record.id))

        second = ConversationMemory(
            revision=2,
            covered_through=4,
            constraints=first.constraints,
        )
        self.assertEqual(
            2,
            restarted.save_conversation_memory(
                record.id, second, next_message_seq=5, expected_revision=1
            ),
        )
        self.assertEqual((second, 5), restarted.load_conversation_memory(record.id))

    def test_v1_database_row_loads_in_memory_and_next_save_upgrades_to_v2(self) -> None:
        """旧行先兼容读取；只有用户后续保存时才升级持久化 schema。"""

        store = SessionStore(self.database, id_factory=lambda: "session-1")
        store.initialize(self.workspace)
        record = store.create("one", self.workspace, "openai", "test")
        v1_payload = {
            "schema_version": 1,
            "revision": 1,
            "generation": 0,
            "covered_through": 2,
            "goal": None,
            "constraints": [],
            "decisions": [],
            "open_items": [
                {
                    "id": "todo",
                    "text": "旧待办",
                    "source_ids": ["m1"],
                    "scope": "session",
                    "task_id": None,
                }
            ],
        }
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO conversation_memory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id, 1, 1, 0, 2, 3,
                    json.dumps(v1_payload, ensure_ascii=False), "now",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        loaded, next_sequence = store.load_conversation_memory(record.id) or (None, None)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(2, loaded.schema_version)
        self.assertEqual("pending", loaded.open_items[0].state)
        self.assertEqual(3, next_sequence)

        store.save_conversation_memory(
            record.id, loaded, next_message_seq=3, expected_revision=1
        )
        connection = sqlite3.connect(self.database)
        try:
            schema_version, payload_json = connection.execute(
                "SELECT schema_version, payload_json FROM conversation_memory WHERE session_id = ?",
                (record.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(2, schema_version)
        self.assertEqual(2, json.loads(payload_json)["schema_version"])

    def test_corrupt_or_unknown_payload_is_not_loaded_or_deleted(self) -> None:
        store = SessionStore(self.database, id_factory=lambda: "session-1")
        store.initialize(self.workspace)
        record = store.create("one", self.workspace, "openai", "test")
        payload = json.loads(memory_to_json(ConversationMemory(revision=1)))
        payload["verification"] = "passed"
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO conversation_memory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    1,
                    1,
                    0,
                    0,
                    1,
                    json.dumps(payload),
                    "now",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SessionError):
            store.load_conversation_memory(record.id)
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversation_memory WHERE session_id = ?", (record.id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, count)

    def test_clear_only_removes_target_sessions_semantic_memory(self) -> None:
        ids = iter(("one", "two"))
        store = SessionStore(self.database, id_factory=lambda: next(ids))
        store.initialize(self.workspace)
        first = store.create("one", self.workspace, "openai", "test")
        second = store.create("two", self.workspace, "openai", "test")
        for record in (first, second):
            store.save_conversation_memory(
                record.id,
                ConversationMemory(revision=1, covered_through=1),
                next_message_seq=2,
                expected_revision=None,
            )

        store.clear_conversation_memory(first.id)

        self.assertIsNone(store.load_conversation_memory(first.id))
        self.assertIsNotNone(store.load_conversation_memory(second.id))


class _NoopAgent:
    pass


class MemoryPersistenceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        self.store = SessionStore(self.database, id_factory=lambda: "session-1")
        self.store.initialize(self.workspace)
        self.record = self.store.create("one", self.workspace, "openai", "test")
        self.saved = ConversationMemory(
            revision=1,
            generation=2,
            covered_through=2,
            constraints=(MemoryItem("api", "不改公共接口", ("m1",), "session"),),
        )
        self.store.save_conversation_memory(
            self.record.id,
            self.saved,
            next_message_seq=3,
            expected_revision=None,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runtime(self, persistence: str) -> SessionRuntime:
        memory_config = MemoryConfig(
            compaction="structured" if persistence == "reviewed_summary" else "off",
            persistence=persistence,
        )
        config = AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig("openai", "test-key", "https://api.openai.com/v1", "test"),
            audit_dir=(self.root / "audit").resolve(),
            memory=memory_config,
        )

        def factory(record, memory, options):  # type: ignore[no-untyped-def]
            return ActiveSession(
                record,
                memory,
                SessionContext(persisted_summary=memory.summary),
                config,
                _NoopAgent(),
            )

        return SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=factory,
        )

    def test_reviewed_mode_restores_intent_but_off_mode_does_not_load_it(self) -> None:
        reviewed = self._runtime("reviewed_summary")
        self.assertEqual(self.saved, reviewed.current.context.conversation_memory)
        self.assertEqual(3, reviewed.current.context.next_message_seq)
        self.assertEqual(1, reviewed.current.context.persisted_memory_revision)
        self.assertEqual("未运行", reviewed.current.context.verification)

        off = self._runtime("off")
        self.assertEqual(ConversationMemory(), off.current.context.conversation_memory)
        self.assertEqual(1, off.current.context.next_message_seq)
        self.assertIsNone(off.current.context.persisted_memory_revision)
        # 关闭功能不自动删除已经确认保存的条目。
        self.assertEqual((self.saved, 3), self.store.load_conversation_memory(self.record.id))

    def test_off_mode_never_persists_in_memory_semantic_candidate(self) -> None:
        self.store.clear_conversation_memory(self.record.id)
        runtime = self._runtime("off")
        runtime.current = replace(
            runtime.current,
            context=replace(
                runtime.current.context,
                conversation_memory=ConversationMemory(
                    revision=1,
                    covered_through=1,
                    goal=MemoryItem("goal", "只留在内存", ("m1",), "session"),
                ),
                next_message_seq=2,
            ),
        )

        self.assertTrue(runtime.retry_persist())
        self.assertIsNone(self.store.load_conversation_memory(self.record.id))

    def test_save_requires_exact_preview_revision_and_rejects_sensitive_candidate(self) -> None:
        runtime = self._runtime("reviewed_summary")
        updated = replace(
            self.saved,
            revision=2,
            open_items=(
                MemoryItem("todo", "补充重启测试", ("m2",), "session", state="pending"),
            ),
        )
        runtime.current = replace(
            runtime.current,
            context=replace(runtime.current.context, conversation_memory=updated),
        )
        preview = runtime.preview_memory_save()
        self.assertIn("补充重启测试", preview.text)
        self.assertIn(str(self.database), preview.text)

        runtime.current = replace(
            runtime.current,
            context=replace(
                runtime.current.context,
                conversation_memory=replace(updated, revision=3),
            ),
        )
        with self.assertRaises(SessionRuntimeError):
            runtime.save_memory_preview(preview)
        self.assertEqual((self.saved, 3), self.store.load_conversation_memory(self.record.id))

        runtime.current = replace(
            runtime.current,
            context=replace(runtime.current.context, conversation_memory=updated),
        )
        runtime.save_memory_preview(runtime.preview_memory_save())
        self.assertEqual((updated, 3), self.store.load_conversation_memory(self.record.id))
        self.assertEqual(2, runtime.current.context.persisted_memory_revision)

        sensitive = replace(
            updated,
            revision=3,
            open_items=(
                MemoryItem(
                    "secret", "api_key=SECRET-SENTINEL", ("m2",), "session",
                    state="pending",
                ),
            ),
        )
        runtime.current = replace(
            runtime.current,
            context=replace(runtime.current.context, conversation_memory=sensitive),
        )
        with self.assertRaises(SessionRuntimeError) as captured:
            runtime.preview_memory_save()
        self.assertNotIn("SECRET-SENTINEL", str(captured.exception))

    def test_local_edit_is_revision_bound_and_does_not_auto_save(self) -> None:
        runtime = self._runtime("reviewed_summary")
        preview = runtime.preview_memory_edit("api", "保持公共接口兼容", "session")
        self.assertIn("保持公共接口兼容", preview.text)

        runtime.apply_memory_edit(preview)

        current = runtime.current.context
        self.assertEqual(2, current.conversation_memory.revision)
        self.assertEqual("保持公共接口兼容", current.conversation_memory.constraints[0].text)
        self.assertEqual(("m3",), current.conversation_memory.constraints[0].source_ids)
        self.assertEqual(4, current.next_message_seq)
        self.assertEqual(2, current.latest_completed_task_seq)
        self.assertEqual((self.saved, 3), self.store.load_conversation_memory(self.record.id))

    def test_local_edit_can_finish_todo_without_promoting_review_candidate(self) -> None:
        """终结状态进入归档；编辑待保存候选时不污染运行时上下文。"""

        runtime = self._runtime("reviewed_summary")
        review = replace(
            self.saved,
            revision=2,
            covered_through=4,
            open_items=(
                MemoryItem("todo", "补生命周期测试", ("m4",), "session", state="pending"),
            ),
        )
        runtime.current = replace(
            runtime.current,
            context=replace(
                runtime.current.context,
                review_memory_candidate=review,
                next_message_seq=5,
            ),
        )

        preview = runtime.preview_memory_edit(
            "todo", "补生命周期测试", "session", "done"
        )
        runtime.apply_memory_edit(preview)

        context = runtime.current.context
        self.assertEqual(self.saved, context.conversation_memory)
        self.assertIsNotNone(context.review_memory_candidate)
        candidate = context.review_memory_candidate
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual((), candidate.open_items)
        self.assertEqual("done", candidate.archived[0].item.state)
        self.assertEqual(3, candidate.revision)
        self.assertEqual(6, context.next_message_seq)

    def test_database_save_failure_keeps_exact_candidate_for_direct_retry(self) -> None:
        runtime = self._runtime("reviewed_summary")
        candidate = replace(
            self.saved,
            revision=2,
            open_items=(
                MemoryItem("todo", "继续验证", ("m2",), "session", state="pending"),
            ),
        )
        runtime.current = replace(
            runtime.current,
            context=replace(runtime.current.context, conversation_memory=candidate),
        )
        preview = runtime.preview_memory_save()

        with mock.patch.object(
            runtime.store,
            "save_conversation_memory",
            side_effect=SessionError("synthetic save failure"),
        ):
            with self.assertRaisesRegex(SessionRuntimeError, "业务工具不会重跑"):
                runtime.save_memory_preview(preview)

        self.assertEqual(candidate, runtime.current.context.conversation_memory)
        self.assertEqual(1, runtime.current.context.persisted_memory_revision)
        runtime.save_memory_preview(preview)
        self.assertEqual((candidate, 3), self.store.load_conversation_memory(self.record.id))

    def test_switching_sessions_never_reuses_another_sessions_memory(self) -> None:
        second_store = SessionStore(self.database, id_factory=lambda: "session-2")
        second = second_store.create("two", self.workspace, "openai", "test")
        second_memory = ConversationMemory(
            revision=1,
            covered_through=4,
            goal=MemoryItem("goal-two", "第二个会话", ("m4",), "session"),
        )
        second_store.save_conversation_memory(
            second.id,
            second_memory,
            next_message_seq=5,
            expected_revision=None,
        )
        runtime = self._runtime("reviewed_summary")

        target_id = self.record.id if runtime.current.record.id == second.id else second.id
        expected = self.saved if target_id == self.record.id else second_memory
        runtime.switch(target_id, confirm=lambda _workspace: True)

        self.assertEqual(expected, runtime.current.context.conversation_memory)
        self.assertEqual(target_id, runtime.current.record.id)

    def test_repeated_decision_replacement_remains_idempotent_after_restart(self) -> None:
        """替代关系持久化后仍可安全重放，不依赖进程内临时状态。"""

        old = MemoryItem("decision-old", "使用旧方案", ("m1",), "session")
        replacement = MemoryItem(
            "decision-new",
            "使用新方案",
            ("m3",),
            "session",
            replaces_id="decision-old",
        )
        before = ConversationMemory(
            revision=1,
            generation=2,
            covered_through=2,
            decisions=(old,),
        )
        first = merge_review_candidate(
            before,
            ConversationMemory(
                revision=before.revision,
                generation=before.generation,
                covered_through=3,
                decisions=(replacement,),
            ),
            allowed_source_ids={"m3"},
        )
        self.store.save_conversation_memory(
            self.record.id,
            first,
            next_message_seq=4,
            expected_revision=1,
        )
        restarted = self._runtime("reviewed_summary")
        restored = restarted.current.context.conversation_memory

        repeated = merge_review_candidate(
            restored,
            ConversationMemory(
                revision=restored.revision,
                generation=restored.generation,
                covered_through=4,
                decisions=(replacement,),
            ),
            allowed_source_ids=set(),
        )

        self.assertEqual((replacement,), repeated.decisions)
        self.assertEqual(first.archived, repeated.archived)
        self.assertEqual(4, repeated.covered_through)


if __name__ == "__main__":
    unittest.main()
