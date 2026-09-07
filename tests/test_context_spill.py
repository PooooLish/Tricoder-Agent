"""大型工具结果 spill 的目录、容量、回读与持久化隔离契约。"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.context import SpillError, ToolResultSpillStore
from tricoder.models import ProviderResponse, SessionMemory, ToolCall, ToolResult
from tricoder.extensions import ToolOrigin
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.sessions import SessionStore
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.handlers import ToolHandler


class ToolResultSpillStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime_root = self.root / "runtime" / "tool-results"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_persist_uses_a_system_generated_name_and_opaque_reference(self) -> None:
        """模型提供的 call id 不能成为文件名或本机路径引用。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")

        record = store.persist("../../outside-secret", "完整正文")
        files = list(self.runtime_root.rglob("*.txt"))

        self.assertEqual(1, len(files))
        self.assertNotIn("outside-secret", files[0].name)
        self.assertNotIn(str(self.runtime_root), record.reference)
        self.assertTrue(record.reference.startswith("spill_"))

    def test_sessions_cannot_read_or_cleanup_each_others_results(self) -> None:
        """引用只在创建它的 session store 内有效，清理也只影响当前会话。"""
        first = ToolResultSpillStore(self.runtime_root, "session-a")
        second = ToolResultSpillStore(self.runtime_root, "session-b")
        first_record = first.persist("call-a", "A" * 100)
        second_record = second.persist("call-b", "B" * 100)

        with self.assertRaisesRegex(SpillError, "引用"):
            second.preview(first_record.reference)
        first.cleanup()

        self.assertEqual("B" * 20, second.preview(second_record.reference, max_chars=20))
        with self.assertRaisesRegex(SpillError, "引用"):
            first.preview(first_record.reference)
        replacement = first.persist("call-c", "C")
        self.assertEqual("C", first.preview(replacement.reference))

    def test_duplicate_call_id_never_overwrites_existing_content(self) -> None:
        """同一调用 ID 的第二份不同正文必须失败，而不是覆盖首份结果。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")
        record = store.persist("call-a", "first")

        with self.assertRaisesRegex(SpillError, "重复"):
            store.persist("call-a", "second")

        self.assertEqual("first", store.preview(record.reference))

    def test_reference_collision_never_overwrites_an_existing_file(self) -> None:
        """随机引用发生竞争碰撞时必须失败关闭并保留原正文。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")
        record = store.persist("call-a", "first")

        with patch.object(store, "_new_reference", return_value=record.reference):
            with self.assertRaises(SpillError):
                store.persist("call-b", "second")

        self.assertEqual("first", store.preview(record.reference))

    def test_entry_and_session_size_limits_fail_closed(self) -> None:
        """单项或累计容量超限时不能继续向运行目录写正文。"""
        store = ToolResultSpillStore(
            self.runtime_root,
            "session-a",
            max_entry_bytes=10,
            max_session_bytes=12,
        )

        with self.assertRaisesRegex(SpillError, "单项"):
            store.persist("too-large", "x" * 11)
        store.persist("first", "12345678")
        with self.assertRaisesRegex(SpillError, "会话"):
            store.persist("second", "12345")

    def test_permission_failure_does_not_expose_content_or_absolute_path(self) -> None:
        """底层写入异常只能转换为固定安全错误。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")
        secret = "do-not-leak"

        with patch("tricoder.context.spill.os.open", side_effect=PermissionError("raw path")):
            with self.assertRaises(SpillError) as captured:
                store.persist("call-a", secret)

        message = str(captured.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(str(self.runtime_root), message)

    def test_symbolic_link_root_is_rejected(self) -> None:
        """运行根目录中的链接不能把正文转移到攻击者控制的位置。"""
        link = self.root / "linked-runtime"
        link.mkdir()
        real_lstat = __import__("os").lstat

        def marked_lstat(path):  # type: ignore[no-untyped-def]
            if Path(path) == link:
                return Mock(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return real_lstat(path)

        with patch("tricoder.context.spill.os.lstat", side_effect=marked_lstat):
            with self.assertRaisesRegex(SpillError, "链接"):
                ToolResultSpillStore(link, "session-a")

    def test_preview_validates_offsets_and_reads_only_a_bounded_chunk(self) -> None:
        """回读必须有界，非法偏移不能退化成任意文件读取。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")
        record = store.persist("call-a", "0123456789")

        self.assertEqual("3456", store.preview(record.reference, offset=3, max_chars=4))
        for offset in (-1, 11):
            with self.subTest(offset=offset):
                with self.assertRaisesRegex(SpillError, "offset"):
                    store.preview(record.reference, offset=offset, max_chars=4)

    def test_cleanup_recreates_a_missing_session_directory(self) -> None:
        """外部清理空目录后，显式 cleanup 仍应恢复可用的受管目录。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")
        store.session_dir.rmdir()

        store.cleanup()
        record = store.persist("call-a", "body")

        self.assertEqual("body", store.preview(record.reference))

    def test_spill_body_and_local_path_are_not_written_to_session_sqlite(self) -> None:
        """会话库 schema 与内容都不能持久化 spill 正文或绝对文件路径。"""
        store = ToolResultSpillStore(self.runtime_root, "session-a")
        secret = "PRIVATE-SPILL-BODY-9f86d081"
        store.persist("call-a", secret)
        database = self.root / "state" / "sessions.db"
        workspace = self.root / "workspace"
        workspace.mkdir()
        sessions = SessionStore(database.resolve(), id_factory=lambda: "session-a")
        sessions.initialize(workspace)
        record = sessions.create("default", workspace, "openai", "model")
        sessions.save_memory(record.id, SessionMemory(summary="safe"))

        raw_database = database.read_bytes()
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()

        self.assertNotIn(secret.encode("utf-8"), raw_database)
        self.assertNotIn(str(self.runtime_root).encode("utf-8"), raw_database)
        self.assertEqual({"sessions", "session_memory"}, tables)


class ToolRegistrySpillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "large.txt").write_text("A" * 200, encoding="utf-8")
        self.store = ToolResultSpillStore(
            self.root / "runtime" / "tool-results",
            "session-a",
        )
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                approver=lambda _action, _detail: True,
                max_output_chars=80,
                spill_store=self.store,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_large_result_becomes_bounded_preview_and_can_be_read_back(self) -> None:
        """模型只接收有界预览和引用，并可通过同一会话分段回读。"""
        result = self.registry.execute(
            "read_file",
            {"path": "large.txt"},
            call_id="call-read",
        )

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.spill_reference)
        self.assertLessEqual(len(result.output), 320)
        self.assertNotIn(str(self.root), result.output)
        self.assertIn("read_tool_result", result.output)
        readback = self.registry.execute(
            "read_tool_result",
            {"reference": result.spill_reference, "offset": 80},
            call_id="call-readback",
        )
        self.assertEqual("A" * 80, readback.output)

    def test_small_result_keeps_the_existing_inline_behavior(self) -> None:
        """未超过工具预算的结果不落盘，也不改变公开内容。"""
        (self.workspace / "small.txt").write_text("hello", encoding="utf-8")

        result = self.registry.execute(
            "read_file",
            {"path": "small.txt"},
            call_id="call-read",
        )

        self.assertEqual("hello", result.output)
        self.assertIsNone(result.spill_reference)

    def test_missing_or_failed_spill_store_enforces_strict_inline_limit(self) -> None:
        """无 store 和落盘失败都必须将截断标记算入总上限，不能返回原始大结果。"""
        class LargeTool(ToolHandler):
            name = "large_probe"
            description = "controlled large output"
            parameters = ToolHandler._schema({}, [])

            def run(self, arguments):
                return ToolResult(True, "X" * 200)

        for store in (None, self.store):
            for limit in (80, 4):
                with self.subTest(store=store is not None, limit=limit):
                    registry = ToolRegistry(ToolContext(
                        WorkspacePolicy(self.workspace), CommandPolicy(self.workspace),
                        approver=lambda *_args: True, max_output_chars=limit, spill_store=store,
                    ))
                    registry.register(LargeTool(registry.context), origin=ToolOrigin("mcp", "probe", "dangerous"))
                    with patch.object(self.store, "persist", side_effect=SpillError("controlled failure")):
                        results = (
                            registry.execute("large_probe", {}),
                            asyncio.run(registry.execute_async("large_probe", {})),
                        )
                    for result in results:
                        self.assertLessEqual(len(result.output), limit)
                        self.assertNotEqual("X" * 200, result.output)
                        self.assertIsNone(result.spill_reference)

    def test_agent_audit_records_only_spill_metadata(self) -> None:
        """审计应包含引用、大小和哈希，但不能包含正文或本机绝对路径。"""
        class Provider:
            def __init__(self) -> None:
                self.responses = [
                    ProviderResponse(
                        tool_calls=(
                            ToolCall("call-read", "read_file", {"path": "large.txt"}),
                        ),
                        finish_reason="tool_calls",
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall("call-finish", "finish", {"summary": "完成"}),
                        ),
                        finish_reason="tool_calls",
                    ),
                ]

            def complete(self, _messages, _tools=()):  # type: ignore[no-untyped-def]
                return self.responses.pop(0)

        audit_path = self.root / "audit" / "run.jsonl"
        CodingAgent(
            Provider(),  # type: ignore[arg-type]
            self.registry,
            max_rounds=2,
            plan_enabled=False,
            audit=AuditLogger(audit_path),
        ).run("读取大文件")

        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertIn('"spill"', audit_text)
        self.assertIn('"sha256"', audit_text)
        self.assertNotIn("A" * 100, audit_text)
        self.assertNotIn(str(self.root), audit_text)


if __name__ == "__main__":
    unittest.main()
