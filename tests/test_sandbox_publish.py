"""执行副本到原项目的显式发布与独立撤销测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.sandbox.publish import SandboxPublishError, SandboxPublisher
from tricoder.sandbox.workspace import SandboxWorkspace
from tricoder.tools.binding import _DirectoryBinding


class SandboxPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original = self.root / "project"
        self.original.mkdir()
        (self.original / "src").mkdir()
        (self.original / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.sandbox = SandboxWorkspace.prepare(
            self.original,
            self.root / "runtime",
            session_id="session-a",
            generation=0,
        )
        self.publisher = SandboxPublisher(self.sandbox)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preview_then_apply_publishes_exact_text_and_second_apply_is_empty(self) -> None:
        copy = self.sandbox.execution_workspace
        (copy / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (copy / "src" / "new.py").write_text("created = True\n", encoding="utf-8")

        preview = self.publisher.prepare()

        self.assertEqual("value = 1\n", (self.original / "src" / "app.py").read_text("utf-8"))
        self.assertEqual(("src/app.py", "src/new.py"), preview.paths)
        self.assertIn("+++ b/src/new.py", preview.diff)

        result = self.publisher.apply(preview)

        self.assertTrue(result.ok)
        self.assertEqual("value = 2\n", (self.original / "src" / "app.py").read_text("utf-8"))
        self.assertEqual("created = True\n", (self.original / "src" / "new.py").read_text("utf-8"))
        with self.assertRaisesRegex(SandboxPublishError, "没有待发布"):
            self.publisher.prepare()

    def test_stale_preview_rejects_copy_or_original_change_without_writing(self) -> None:
        target = self.sandbox.execution_workspace / "src" / "app.py"
        target.write_text("value = 2\n", encoding="utf-8")
        preview = self.publisher.prepare()
        target.write_text("value = 3\n", encoding="utf-8")
        with self.assertRaisesRegex(SandboxPublishError, "过期"):
            self.publisher.apply(preview)
        self.assertEqual("value = 1\n", (self.original / "src" / "app.py").read_text("utf-8"))

        preview = self.publisher.prepare()
        (self.original / "src" / "app.py").write_text("external = True\n", encoding="utf-8")
        with self.assertRaisesRegex(SandboxPublishError, "冲突"):
            self.publisher.apply(preview)
        self.assertEqual("external = True\n", (self.original / "src" / "app.py").read_text("utf-8"))

    def test_preview_cannot_cross_session_or_publisher_authority(self) -> None:
        target = self.sandbox.execution_workspace / "src" / "app.py"
        target.write_text("value = 2\n", encoding="utf-8")
        preview = self.publisher.prepare()
        other = SandboxWorkspace.prepare(
            self.original,
            self.root / "runtime",
            session_id="session-other",
            generation=0,
        )

        with self.assertRaisesRegex(SandboxPublishError, "当前 Session"):
            SandboxPublisher(other).apply(preview)

        self.assertEqual("value = 1\n", (self.original / "src" / "app.py").read_text("utf-8"))

    def test_delete_binary_and_new_parent_are_rejected_as_one_publish(self) -> None:
        copy = self.sandbox.execution_workspace
        (copy / "src" / "app.py").unlink()
        with self.assertRaisesRegex(SandboxPublishError, "删除"):
            self.publisher.prepare()

        # 使用新的 generation，避免把已删除的草稿误当成干净副本。
        second = SandboxWorkspace.prepare(
            self.original,
            self.root / "runtime",
            session_id="session-b",
            generation=0,
        )
        (second.execution_workspace / "binary.dat").write_bytes(b"\x00\xff")
        with self.assertRaisesRegex(SandboxPublishError, "UTF-8|二进制"):
            SandboxPublisher(second).prepare()

        third = SandboxWorkspace.prepare(
            self.original,
            self.root / "runtime",
            session_id="session-c",
            generation=0,
        )
        (third.execution_workspace / "new-dir").mkdir()
        (third.execution_workspace / "new-dir" / "file.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(SandboxPublishError, "父目录"):
            SandboxPublisher(third).prepare()

    def test_publish_undo_is_separate_and_republish_becomes_available(self) -> None:
        target = self.sandbox.execution_workspace / "src" / "app.py"
        target.write_text("value = 2\n", encoding="utf-8")
        self.assertTrue(self.publisher.apply(self.publisher.prepare()).ok)

        undo_preview = self.publisher.prepare_undo()
        self.assertIn("-value = 2", undo_preview.diff)
        self.assertTrue(self.publisher.undo(undo_preview).ok)
        self.assertEqual("value = 1\n", (self.original / "src" / "app.py").read_text("utf-8"))

        republish = self.publisher.prepare()
        self.assertEqual(("src/app.py",), republish.paths)

    def test_partial_publish_failure_compensates_already_written_files(self) -> None:
        (self.original / "src" / "other.py").write_text("other = 1\n", encoding="utf-8")
        sandbox = SandboxWorkspace.prepare(
            self.original,
            self.root / "runtime",
            session_id="session-compensate",
            generation=0,
        )
        copy = sandbox.execution_workspace
        (copy / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (copy / "src" / "other.py").write_text("other = 2\n", encoding="utf-8")
        publisher = SandboxPublisher(sandbox)
        preview = publisher.prepare()

        probe = _DirectoryBinding.open(self.original, self.original / "src")
        binding_type = type(probe)
        probe.close()
        real_replace = binding_type.replace

        def fail_other(binding, temporary_name: str, target_name: str):  # type: ignore[no-untyped-def]
            if target_name == "other.py":
                raise OSError("synthetic publish failure")
            return real_replace(binding, temporary_name, target_name)

        with patch.object(binding_type, "replace", fail_other):
            result = publisher.apply(preview)

        self.assertFalse(result.ok)
        self.assertEqual((), result.compensation_failed)
        self.assertEqual("value = 1\n", (self.original / "src" / "app.py").read_text("utf-8"))
        self.assertEqual("other = 1\n", (self.original / "src" / "other.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
