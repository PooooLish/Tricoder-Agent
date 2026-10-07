"""历史验证义务与本轮交付门禁的恢复回归。"""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    ProviderResponse,
    SessionMemory,
    ToolCall,
)
from tricoder.session.runtime import RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionError, SessionStore


class _QueueProvider:
    """只返回合成响应；队列耗尽即暴露隐藏的额外模型请求。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        if not self._responses:
            raise AssertionError("Provider 响应队列已耗尽")
        return self._responses.pop(0)


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(ToolCall(call_id, name, arguments),),
        finish_reason="tool_calls",
    )


def _finish(call_id: str, summary: str = "已完成只读回顾") -> ProviderResponse:
    return _call(
        call_id,
        "finish",
        {"summary": summary, "outcome": "completed"},
    )


class VerificationObligationRecoveryTests(unittest.TestCase):
    """R0：实际恢复入口不得把历史展示状态变成本轮修改义务。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self.database = (self.root / "state" / "sessions.db").resolve()
        store = SessionStore(self.database, id_factory=lambda: "verification-scope")
        store.initialize(self.workspace)
        self.record = store.create(
            "verification-scope",
            self.workspace,
            "openai",
            "synthetic-model",
        )

    def _config(self) -> AppConfig:
        return AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                "openai",
                "synthetic-test-key",
                "https://example.test/v1",
                "synthetic-model",
            ),
            audit_dir=(self.root / "state" / "audit").resolve(),
            plan_enabled=False,
            memory=MemoryConfig(compaction="off", persistence="off"),
        )

    def _runtime(self, responses: list[ProviderResponse]) -> SessionRuntime:
        provider = _QueueProvider(responses)
        runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=lambda **_kwargs: self._config(),
            provider_factory=lambda _config, _timeout: provider,
            initial_session_id=self.record.id,
            approver=lambda _action, _detail: True,
            workspace_confirmer=lambda _preview: True,
        )
        return runtime

    def _restart_and_review(self) -> tuple[SessionRuntime, object]:
        restarted = self._runtime([_finish("review-finish")])
        result = restarted.run_task("回顾一下之前做了什么，不修改任何文件")
        return restarted, result

    def test_failed_check_history_does_not_create_review_task_obligation(self) -> None:
        first = self._runtime([
            _call(
                "failed-check",
                "run_command",
                {"command": "python -m compileall -q broken.py"},
            ),
            _finish("first-finish", "已报告语法检查失败"),
        ])
        try:
            initial = first.run_task("只读检查 broken.py 并报告结果")
            self.assertTrue(initial.ok, initial.summary)
            self.assertEqual("failed", first.current.memory.verification)
        finally:
            self.assertTrue(first.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual("failed", restarted.current.memory.verification)
            self.assertFalse(restarted.current.context.verification_required)
        finally:
            restarted.close()

    def test_passed_check_history_does_not_create_review_task_obligation(self) -> None:
        first = self._runtime([
            _call(
                "passed-check",
                "run_command",
                {"command": "python -m compileall -q app.py"},
            ),
            _finish("first-finish", "只读检查已完成"),
        ])
        try:
            initial = first.run_task("只读检查 app.py")
            self.assertTrue(initial.ok, initial.summary)
            self.assertEqual("passed", first.current.memory.verification)
        finally:
            self.assertTrue(first.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertFalse(restarted.current.context.verification_required)
            self.assertIsNone(restarted.current.context.verification_evidence)
        finally:
            restarted.close()

    def test_pending_modified_history_survives_successful_read_only_review(self) -> None:
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("first-finish", "文件已修改"),
        ])
        try:
            changed = first.run_task("把 app.py 中的值改为 2")
            self.assertFalse(changed.ok)
            self.assertEqual(("app.py",), first.current.memory.modified_files)
            self.assertEqual("pending", first.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), first.current.memory.pending_verification_paths
            )
        finally:
            self.assertTrue(first.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual(("app.py",), restarted.current.memory.modified_files)
            self.assertEqual("待验证", restarted.current.memory.verification)
            self.assertEqual(
                "pending", restarted.current.memory.verification_obligation
            )
            self.assertEqual(
                ("app.py",), restarted.current.memory.pending_verification_paths
            )
            self.assertFalse(restarted.current.context.verification_required)
        finally:
            restarted.close()

    def test_current_edit_still_requires_verification_and_net_zero_is_not_review(self) -> None:
        runtime = self._runtime([
            _call(
                "edit-forward",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _call(
                "edit-back",
                "edit_file",
                {"path": "app.py", "old_text": "x = 2", "new_text": "x = 1"},
            ),
            _finish("finish-net-zero", "已恢复原内容"),
        ])
        try:
            result = runtime.run_task("修改后再恢复 app.py")
            self.assertFalse(result.ok)
            self.assertEqual("pending", runtime.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), runtime.current.memory.pending_verification_paths
            )
        finally:
            runtime.close()

    def test_same_process_review_does_not_reuse_previous_task_gate(self) -> None:
        runtime = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
            _finish("finish-review", "已回顾未验证修改"),
        ])
        try:
            self.assertFalse(runtime.run_task("修改 app.py").ok)
            reviewed = runtime.run_task("回顾刚才做了什么，不修改文件")
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertFalse(reviewed.current_verification_required)
            self.assertEqual("pending", reviewed.verification_obligation)
            self.assertEqual(("app.py",), reviewed.pending_verification_paths)
        finally:
            runtime.close()

    def test_current_directory_creation_establishes_current_and_historical_obligation(self) -> None:
        runtime = self._runtime([
            _call("create-dir", "create_directory", {"path": "generated"}),
            _finish("finish-dir", "目录已创建"),
        ])
        try:
            result = runtime.run_task("创建 generated 目录")
            self.assertFalse(result.ok)
            self.assertTrue(result.current_verification_required)
            self.assertEqual("pending", result.verification_obligation)
            self.assertEqual(("generated",), result.pending_verification_paths)
        finally:
            runtime.close()

    def test_relevant_host_check_can_clear_only_covered_pending_path(self) -> None:
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-app",
                "run_command",
                {"command": "python -m compileall -q app.py"},
            ),
            _finish("finish-check", "已检查历史修改"),
        ])
        try:
            result = checked.run_task("检查 app.py 的历史修改")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("none", checked.current.memory.verification_obligation)
            self.assertEqual((), checked.current.memory.pending_verification_paths)
        finally:
            checked.close()

    def test_unrelated_host_check_does_not_clear_pending_path(self) -> None:
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-other",
                "run_command",
                {"command": "python -m compileall -q broken.py"},
            ),
            _finish("finish-check", "已报告无关检查失败"),
        ])
        try:
            result = checked.run_task("只读检查另一个文件")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("pending", checked.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), checked.current.memory.pending_verification_paths
            )
        finally:
            checked.close()

    def test_legacy_unknown_survives_review_without_becoming_current_gate(self) -> None:
        store = SessionStore(self.database)
        memory = store.load_memory(self.record.id)
        store.save_memory(
            self.record.id,
            SessionMemory(
                summary=memory.summary,
                requirements_summary=memory.requirements_summary,
                last_task_summary=memory.last_task_summary,
                modified_files=memory.modified_files,
                verification="failed",
                permission_level=memory.permission_level,
                verification_obligation="legacy_unknown",
            ),
        )

        restarted, result = self._restart_and_review()
        try:
            self.assertTrue(result.ok, result.summary)
            self.assertFalse(result.current_verification_required)
            self.assertEqual("legacy_unknown", result.verification_obligation)
            self.assertEqual(
                "legacy_unknown", restarted.current.memory.verification_obligation
            )
        finally:
            restarted.close()


class VerificationObligationStoreTests(unittest.TestCase):
    """R1：验证义务元数据使用追加迁移并严格校验。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        self.store = SessionStore(self.database, id_factory=lambda: "store-session")
        self.store.initialize(self.workspace)
        self.record = self.store.create(
            "store-session", self.workspace, "openai", "synthetic-model"
        )

    def test_new_session_defaults_and_pending_paths_roundtrip(self) -> None:
        created = self.store.load_memory(self.record.id)
        self.assertEqual("none", created.verification_obligation)
        self.assertEqual((), created.pending_verification_paths)

        pending = SessionMemory(
            modified_files=("src/app.py", "tests/test_app.py"),
            verification="待验证",
            verification_obligation="pending",
            pending_verification_paths=("src/app.py", "tests/test_app.py"),
        )
        self.store.save_memory(self.record.id, pending)

        self.assertEqual(pending, self.store.load_memory(self.record.id))

    def test_old_schema_migrates_obligations_conservatively_and_idempotently(self) -> None:
        legacy_database = (self.root / "legacy" / "sessions.db").resolve()
        legacy_database.parent.mkdir(parents=True)
        connection = sqlite3.connect(legacy_database)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE session_memory (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    requirements_summary TEXT NOT NULL DEFAULT '',
                    last_task_summary TEXT NOT NULL DEFAULT '',
                    modified_files_json TEXT NOT NULL DEFAULT '[]',
                    verification TEXT NOT NULL DEFAULT '未运行',
                    permission TEXT NOT NULL DEFAULT 'strict',
                    unknown_effects INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                """
            )
            cases = (
                ("empty", "未运行", "[]", 0),
                ("passed", "passed", "[]", 0),
                ("failed", "failed", "[]", 0),
                ("pending-path", "待验证", '["src/app.py"]', 0),
                ("unknown", "未运行", "[]", 1),
            )
            for session_id, verification, files_json, unknown in cases:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        session_id,
                        str(self.workspace),
                        "openai",
                        "synthetic-model",
                        "2026-10-07T00:00:00+00:00",
                        "2026-10-07T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO session_memory (
                        session_id, modified_files_json, verification, unknown_effects
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session_id, files_json, verification, unknown),
                )
            connection.commit()
        finally:
            connection.close()

        migrated = SessionStore(legacy_database)
        migrated.initialize(self.workspace)
        migrated.initialize(self.workspace)

        self.assertEqual("none", migrated.load_memory("empty").verification_obligation)
        self.assertEqual(
            "legacy_unknown",
            migrated.load_memory("passed").verification_obligation,
        )
        self.assertEqual(
            "legacy_unknown",
            migrated.load_memory("failed").verification_obligation,
        )
        pending = migrated.load_memory("pending-path")
        self.assertEqual("pending", pending.verification_obligation)
        self.assertEqual(("src/app.py",), pending.pending_verification_paths)
        unknown = migrated.load_memory("unknown")
        self.assertEqual("legacy_unknown", unknown.verification_obligation)
        self.assertTrue(unknown.unknown_effects)

    def test_corrupted_or_unsafe_obligation_metadata_is_rejected_atomically(self) -> None:
        original = self.store.load_memory(self.record.id)
        with self.assertRaises(SessionError):
            self.store.save_memory(
                self.record.id,
                SessionMemory(
                    verification_obligation="pending",
                    pending_verification_paths=("../outside.py",),
                ),
            )
        self.assertEqual(original, self.store.load_memory(self.record.id))

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE session_memory
                SET verification_obligation = ?, pending_verification_paths_json = ?
                WHERE session_id = ?
                """,
                ("invalid", "{not-json", self.record.id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SessionError):
            self.store.load_memory(self.record.id)


if __name__ == "__main__":
    unittest.main()
