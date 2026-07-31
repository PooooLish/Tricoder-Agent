import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.models import SessionMemory
from tricoder.sessions import (
    SessionError,
    SessionStore,
    default_sessions_db,
    safe_requirement_summary,
    safe_result_summary,
    validate_session_name,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.db_path = (self.root / "state" / "sessions.db").resolve()
        self.times = iter(
            [
                "2026-07-31T00:00:00+00:00",
                "2026-07-31T00:01:00+00:00",
                "2026-07-31T00:02:00+00:00",
                "2026-07-31T00:03:00+00:00",
            ]
        )
        self.identifiers = iter(["session-one", "session-two"])
        self.store = SessionStore(
            self.db_path,
            clock=lambda: next(self.times),
            id_factory=lambda: next(self.identifiers),
        )
        self.store.initialize(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_create_list_rename_and_memory_are_isolated(self) -> None:
        """防止一个会话的记忆或更新时间串入另一个会话。"""
        first = self.store.create("one", self.workspace, "deepseek", "model-a")
        second = self.store.create("two", self.workspace, "glm", "model-b")

        self.store.save_memory(first.id, SessionMemory(summary="first"))
        renamed = self.store.rename(second.id, "renamed")

        self.assertEqual("first", self.store.load_memory(first.id).summary)
        self.assertEqual("", self.store.load_memory(second.id).summary)
        self.assertEqual("2026-07-31T00:03:00+00:00", renamed.updated_at)
        self.assertEqual(["renamed", "one"], [item.name for item in self.store.list_all()])
        self.assertEqual(second.id, self.store.latest_for_workspace(self.workspace).id)

    def test_prepared_record_has_no_database_side_effect_until_inserted(self) -> None:
        """防止候选装配前的 ID 预分配意外创建会话或空记忆记录。"""
        prepared = self.store.prepare_record("prepared", self.workspace, "glm", "model-c")

        self.assertEqual([], self.store.list_all())
        inserted = self.store.insert_prepared(prepared)

        self.assertEqual(prepared, inserted)
        self.assertEqual(SessionMemory(), self.store.load_memory(prepared.id))

    def test_clear_memory_keeps_session_metadata(self) -> None:
        """防止清空记忆时误删除会话配置或修改文件列表。"""
        created = self.store.create("keep", self.workspace, "deepseek", "model-a")
        self.store.save_memory(
            created.id,
            SessionMemory(
                summary="summary",
                requirements_summary="requirements",
                last_task_summary="task",
                modified_files=("src/app.py",),
                verification="passed",
            ),
        )

        self.store.clear_memory(created.id)

        restored = self.store.get(created.id)
        self.assertEqual(created.id, restored.id)
        self.assertEqual(created.name, restored.name)
        self.assertEqual(created.workspace, restored.workspace)
        self.assertEqual(created.provider, restored.provider)
        self.assertEqual(created.model, restored.model)
        self.assertEqual(SessionMemory(), self.store.load_memory(created.id))

    def test_invalid_memory_file_list_rolls_back_existing_memory(self) -> None:
        """防止非字符串 JSON 项写入后破坏已有的会话记忆。"""
        created = self.store.create("stable", self.workspace, "deepseek", "model-a")
        self.store.save_memory(created.id, SessionMemory(summary="kept"))

        with self.assertRaises(SessionError):
            self.store.save_memory(
                created.id,
                SessionMemory(summary="replacement", modified_files=("src/app.py", 7)),  # type: ignore[arg-type]
            )

        self.assertEqual("kept", self.store.load_memory(created.id).summary)

    def test_load_memory_rejects_corrupted_file_list_json(self) -> None:
        """防止损坏的持久化数据被静默覆盖为默认记忆。"""
        created = self.store.create("broken", self.workspace, "deepseek", "model-a")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE session_memory SET modified_files_json = ? WHERE session_id = ?",
                ("{not-json", created.id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SessionError):
            self.store.load_memory(created.id)

    def test_rename_with_invalid_name_preserves_existing_record(self) -> None:
        """防止无效名称导致已有会话被部分更新。"""
        created = self.store.create("original", self.workspace, "deepseek", "model-a")

        with self.assertRaises(SessionError):
            self.store.rename(created.id, "bad\nname")

        self.assertEqual("original", self.store.get(created.id).name)

    def test_create_rejects_database_inside_target_workspace(self) -> None:
        """防止跨工作区切换后向新目标工作区写入会话数据库。"""
        unsafe_workspace = self.root

        with self.assertRaises(SessionError):
            self.store.create("unsafe", unsafe_workspace, "deepseek", "model-a")

        self.assertEqual([], self.store.list_all())

    def test_initialize_rejects_database_inside_workspace_before_writing(self) -> None:
        """防止初始化先创建工作区内的状态目录或 SQLite 文件。"""
        state_dir = self.workspace / "runtime"
        database_path = state_dir / "sessions.db"
        store = SessionStore(database_path.resolve())

        with self.assertRaises(SessionError):
            store.initialize(self.workspace)

        self.assertFalse(state_dir.exists())
        self.assertFalse(database_path.exists())

    def test_load_memory_rejects_non_text_scalar_column(self) -> None:
        """防止 SQLite BLOB 被误当作可恢复的文本摘要。"""
        created = self.store.create("memory-types", self.workspace, "deepseek", "model-a")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE session_memory SET summary = ? WHERE session_id = ?",
                (sqlite3.Binary(b"not-text"), created.id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SessionError):
            self.store.load_memory(created.id)

    def test_get_rejects_non_text_scalar_column(self) -> None:
        """防止 SQLite BLOB 被误当作会话元数据字段。"""
        created = self.store.create("record-types", self.workspace, "deepseek", "model-a")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE sessions SET provider = ? WHERE id = ?",
                (sqlite3.Binary(b"not-text"), created.id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SessionError):
            self.store.get(created.id)


class SessionPathAndSafetyTests(unittest.TestCase):
    def test_default_sessions_db_uses_windows_local_app_data(self) -> None:
        """防止 Windows 将状态数据库误写入当前工作区。"""
        with patch("tricoder.sessions._is_windows", return_value=True):
            path = default_sessions_db({"LOCALAPPDATA": "D:/state"})

        self.assertEqual(Path("D:/state/TriCoder/sessions.db").resolve(), path)

    def test_windows_fallback_prefers_supplied_userprofile(self) -> None:
        """防止注入环境缺少 LOCALAPPDATA 时误用当前进程账户的主目录。"""
        with (
            patch("tricoder.sessions._is_windows", return_value=True),
            patch("tricoder.sessions.Path.home", return_value=Path("Z:/process-home")),
        ):
            path = default_sessions_db({"USERPROFILE": "D:/users/test-user"})

        self.assertEqual(
            Path("D:/users/test-user/AppData/Local/TriCoder/sessions.db").resolve(),
            path,
        )

    def test_default_sessions_db_uses_xdg_state_home(self) -> None:
        """防止非 Windows 平台忽略 XDG 状态目录。"""
        with patch("tricoder.sessions._is_windows", return_value=False):
            path = default_sessions_db({"XDG_STATE_HOME": "/var/state"})

        self.assertEqual(Path("/var/state/tricoder/sessions.db").resolve(), path)

    def test_validate_session_name_rejects_control_characters(self) -> None:
        """防止控制字符或空名称进入终端列表和 SQLite。"""
        for value in ("", " ", "bad\nname", "x" * 51):
            with self.subTest(value=value):
                with self.assertRaises(SessionError):
                    validate_session_name(value)

    def test_safe_requirement_never_persists_free_text(self) -> None:
        """防止任务原文无论形态如何进入 SQLite。"""
        values = ("", "修复登录问题", "summarize the change", "ls -la", "Get-ChildItem -Force", "echo note", "x = 1", "foo()")

        for value in values:
            with self.subTest(value=value):
                summary = safe_requirement_summary(value)
                self.assertIn("原文未持久化", summary)
                if value:
                    self.assertNotIn(value, summary)
                self.assertIn(str(len(value)), summary)

    def test_safe_result_never_persists_free_text(self) -> None:
        """防止 finish 摘要无论是自然语言、命令、源码或 JSON 都原样进入 SQLite。"""
        values = (
            "",
            "测试完成",
            "verification completed",
            "ls -la",
            "Get-ChildItem -Force",
            "echo note",
            "x = 1",
            "foo()",
            '{"tool":"read_file","arguments":{"path":"private.py"},"reason":"inspect"}',
        )

        for value in values:
            with self.subTest(value=value):
                summary = safe_result_summary(value)
                self.assertIn("原文未持久化", summary)
                if value:
                    self.assertNotIn(value, summary)
                self.assertIn(str(len(value)), summary)


if __name__ == "__main__":
    unittest.main()
