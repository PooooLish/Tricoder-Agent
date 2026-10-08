"""SessionStore 的持久化工作区基线迁移、CAS 与损坏区分。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tricoder.models import SessionMemory
from tricoder.session.store import (
    SessionStore,
    WorkspaceBaselineStoreError,
)
from tricoder.workspace.baseline_record import (
    MAX_BASELINE_RECORD_PAYLOAD_BYTES,
    make_baseline_record,
)
from tricoder.workspace.snapshot import capture_workspace_baseline


class WorkspaceBaselineStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.database = self.root / "state" / "sessions.db"
        self.store = SessionStore(self.database)
        self.store.initialize(self.workspace)
        self.first = self.store.create("first", self.workspace, "openai", "model")
        self.second = self.store.create("second", self.workspace, "openai", "model")

    def _record(self):  # type: ignore[no-untyped-def]
        return make_baseline_record(capture_workspace_baseline(self.workspace))

    def _replace_payload(self, session_id: str, payload: str, *, version: int = 1) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE session_workspace_baselines "
                "SET format_version = ?, payload_json = ? WHERE session_id = ?",
                (version, payload, session_id),
            )

    def test_initial_insert_round_trip_cas_and_session_isolation(self) -> None:
        """旧 revision 或另一 Session 不能覆盖当前 Session 的认可清单。"""

        before_memory = SessionMemory(
            summary="safe",
            verification="failed",
            verification_obligation="legacy_unknown",
        )
        self.store.save_memory(self.first.id, before_memory)
        initial = self._record()

        self.assertIsNone(self.store.load_workspace_baseline(self.first.id))
        stored = self.store.save_workspace_baseline(
            self.first.id, initial, expected_revision=None
        )
        self.assertEqual(1, stored.revision)
        self.assertEqual(initial, stored.record)
        self.assertEqual(stored, self.store.load_workspace_baseline(self.first.id))
        self.assertIsNone(self.store.load_workspace_baseline(self.second.id))
        self.assertEqual(before_memory, self.store.load_memory(self.first.id))

        (self.workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        updated = self._record()
        advanced = self.store.save_workspace_baseline(
            self.first.id, updated, expected_revision=stored.revision
        )
        self.assertEqual(2, advanced.revision)
        with self.assertRaises(WorkspaceBaselineStoreError) as captured:
            self.store.save_workspace_baseline(
                self.first.id, initial, expected_revision=stored.revision
            )
        self.assertEqual("revision_conflict", captured.exception.reason)
        self.assertEqual(advanced, self.store.load_workspace_baseline(self.first.id))

    def test_initial_save_rolls_back_record_and_marker_together(self) -> None:
        """首次插入任一步失败都不能留下 marker/记录半完成状态。"""

        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER reject_baseline_marker
                BEFORE UPDATE OF workspace_baseline_initialized ON sessions
                WHEN NEW.id = '%s'
                BEGIN SELECT RAISE(ABORT, 'reject'); END
                """ % self.first.id
            )
        with self.assertRaises(WorkspaceBaselineStoreError):
            self.store.save_workspace_baseline(
                self.first.id, self._record(), expected_revision=None
            )
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TRIGGER reject_baseline_marker")

        self.assertIsNone(self.store.load_workspace_baseline(self.first.id))

    def test_initialized_marker_and_record_must_remain_consistent(self) -> None:
        """缺失记录和孤立记录都不是可自动覆盖的首次使用。"""

        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE sessions SET workspace_baseline_initialized = 1 WHERE id = ?",
                (self.first.id,),
            )
        with self.assertRaises(WorkspaceBaselineStoreError) as missing:
            self.store.load_workspace_baseline(self.first.id)
        self.assertEqual("missing_record", missing.exception.reason)

        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE sessions SET workspace_baseline_initialized = 0 WHERE id = ?",
                (self.first.id,),
            )
        self.store.save_workspace_baseline(
            self.first.id, self._record(), expected_revision=None
        )
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE sessions SET workspace_baseline_initialized = 0 WHERE id = ?",
                (self.first.id,),
            )
        with self.assertRaises(WorkspaceBaselineStoreError) as orphaned:
            self.store.load_workspace_baseline(self.first.id)
        self.assertEqual("inconsistent_record", orphaned.exception.reason)

    def test_corrupted_unsupported_incomplete_and_oversized_payloads_are_distinct(self) -> None:
        """坏记录不能被 load 折叠为 None 后静默重建。"""

        cases = (
            ("corrupted", "{", 1),
            (
                "unsupported_format",
                json.dumps({"format_version": 99}),
                99,
            ),
            (
                "incomplete",
                None,
                1,
            ),
            (
                "limit_exceeded",
                "x" * (MAX_BASELINE_RECORD_PAYLOAD_BYTES + 1),
                1,
            ),
        )
        for index, (reason, raw_payload, version) in enumerate(cases):
            with self.subTest(reason=reason):
                session = self.store.create(
                    f"broken-{index}", self.workspace, "openai", "model"
                )
                self.store.save_workspace_baseline(
                    session.id, self._record(), expected_revision=None
                )
                if raw_payload is None:
                    with closing(sqlite3.connect(self.database)) as connection, connection:
                        payload = json.loads(connection.execute(
                            "SELECT payload_json FROM session_workspace_baselines "
                            "WHERE session_id = ?",
                            (session.id,),
                        ).fetchone()[0])
                    payload["complete"] = False
                    raw_payload = json.dumps(payload)
                self._replace_payload(session.id, raw_payload, version=version)

                with self.assertRaises(WorkspaceBaselineStoreError) as captured:
                    self.store.load_workspace_baseline(session.id)
                self.assertEqual(reason, captured.exception.reason)

    def test_migration_is_idempotent_and_does_not_invent_old_baselines(self) -> None:
        """旧 sessions 行迁移后 marker 仍为 false，重复初始化不改原数据。"""

        legacy_database = self.root / "legacy" / "sessions.db"
        legacy_database.parent.mkdir()
        with closing(sqlite3.connect(legacy_database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy", "legacy", str(self.workspace.resolve()), "openai",
                    "model", "created", "updated",
                ),
            )
        legacy = SessionStore(legacy_database)

        legacy.initialize(self.workspace)
        legacy.initialize(self.workspace)

        self.assertEqual("legacy", legacy.get("legacy").id)
        self.assertIsNone(legacy.load_workspace_baseline("legacy"))
        with closing(sqlite3.connect(legacy_database)) as connection, connection:
            marker = connection.execute(
                "SELECT workspace_baseline_initialized FROM sessions WHERE id = 'legacy'"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT COUNT(*) FROM session_workspace_baselines"
            ).fetchone()[0]
        self.assertEqual(0, marker)
        self.assertEqual(0, rows)

    def test_stored_payload_contains_no_source_or_tool_output(self) -> None:
        """持久化表只接收 T1 纯数据，不保存扫描正文或工具原文。"""

        source = "SOURCE-BODY-SENTINEL"
        (self.workspace / "app.py").write_text(source, encoding="utf-8")
        self.store.save_workspace_baseline(
            self.first.id, self._record(), expected_revision=None
        )
        with closing(sqlite3.connect(self.database)) as connection, connection:
            payload = connection.execute(
                "SELECT payload_json FROM session_workspace_baselines WHERE session_id = ?",
                (self.first.id,),
            ).fetchone()[0]

        self.assertNotIn(source, payload)
        self.assertNotIn("tool_output", payload)


if __name__ == "__main__":
    unittest.main()
