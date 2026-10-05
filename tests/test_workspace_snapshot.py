"""任务前工作区内容基线、差异和失败关闭测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tricoder.workspace.snapshot import (
    FileSnapshotEntry,
    SnapshotLimits,
    WorkspaceScanError,
    capture_workspace_baseline,
    compare_baselines,
)


class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        self.workspace.mkdir()
        self.limits = SnapshotLimits(timeout_seconds=3.0)

    def capture(self):  # type: ignore[no-untyped-def]
        return capture_workspace_baseline(self.workspace, self.limits)

    def test_same_size_same_mtime_content_change_is_detected(self) -> None:
        """若扫描只信 size/mtime，等长内容替换会被误判为无变化。"""

        path = self.workspace / "same.py"
        path.write_text("one\n", encoding="utf-8")
        before = self.capture()
        timestamp = path.stat().st_mtime_ns
        path.write_text("two\n", encoding="utf-8")
        os.utime(path, ns=(timestamp, timestamp))

        after = self.capture()
        preview = compare_baselines(before, after)

        self.assertTrue(preview.changed)
        self.assertEqual(("same.py",), preview.changed_paths)
        self.assertIn("-one", preview.full_diff)
        self.assertIn("+two", preview.full_diff)

    def test_added_deleted_empty_and_binary_files_have_complete_preview(self) -> None:
        """若不可展示正文的文件被省略，确认对象并未覆盖完整候选。"""

        (self.workspace / "delete.txt").write_text("old\n", encoding="utf-8")
        (self.workspace / "empty.txt").write_text("", encoding="utf-8")
        (self.workspace / "blob.bin").write_bytes(b"\x00old")
        before = self.capture()
        (self.workspace / "delete.txt").unlink()
        (self.workspace / "empty.txt").write_text("now\n", encoding="utf-8")
        (self.workspace / "blob.bin").write_bytes(b"\x00new")
        (self.workspace / "added.txt").write_text("added\n", encoding="utf-8")
        after = self.capture()

        preview = compare_baselines(before, after, page_chars=120)

        self.assertEqual(
            ("added.txt", "blob.bin", "delete.txt", "empty.txt"),
            preview.changed_paths,
        )
        self.assertGreater(len(preview.pages), 1)
        self.assertEqual(preview.full_diff, "".join(preview.pages))
        self.assertIn("二进制", preview.full_diff)
        self.assertIn("added.txt", preview.full_diff)
        self.assertIn("delete.txt", preview.full_diff)

    def test_sensitive_and_control_paths_are_excluded_before_content_read(self) -> None:
        """若排除发生在打开后，扫描仍可能把真实凭据读入进程内存。"""

        # 模拟 Windows 临时目录别名：调用方路径与 resolve 后路径指向
        # 同一目录，但字符串不同。无需依赖机器是否启用 8.3 短文件名。
        self.workspace = self.workspace / ".." / self.workspace.name
        (self.workspace / ".env.local").write_text("SECRET=must-not-read", encoding="utf-8")
        control = self.workspace / "runtime" / "tricoder-control"
        control.mkdir(parents=True)
        (control / "workspace.lock").write_text("internal", encoding="utf-8")
        (self.workspace / "runtime" / "user_code.py").write_text("value = 1\n", encoding="utf-8")

        from tricoder.workspace import snapshot as workspace_snapshot

        original = workspace_snapshot._read_entry
        canonical_workspace = self.workspace.resolve(strict=True)

        def guarded(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            relative = path.relative_to(canonical_workspace).as_posix()
            if relative in {".env.local", "runtime/tricoder-control/workspace.lock"}:
                self.fail(f"排除路径被打开：{relative}")
            return original(path, *args, **kwargs)

        with mock.patch.object(workspace_snapshot, "_read_entry", side_effect=guarded):
            baseline = self.capture()

        paths = tuple(entry.path for entry in baseline.entries)
        self.assertNotIn(".env.local", paths)
        self.assertFalse(any(path.startswith("runtime/tricoder-control") for path in paths))
        self.assertIn("runtime/user_code.py", paths)

    def test_any_limit_or_unstable_scan_raises_fixed_safe_error(self) -> None:
        """若超限返回部分 snapshot，门禁可能把未扫描内容当成无变化。"""

        (self.workspace / "a.py").write_text("a", encoding="utf-8")
        (self.workspace / "b.py").write_text("b", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceScanError, "limit_exceeded"):
            capture_workspace_baseline(
                self.workspace,
                replace(self.limits, max_files=1),
            )

        from tricoder.workspace import snapshot as workspace_snapshot

        calls = 0
        original = workspace_snapshot._inventory

        def changing(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.workspace / "late.py").write_text("late", encoding="utf-8")
            return result

        with mock.patch.object(workspace_snapshot, "_inventory", side_effect=changing):
            with self.assertRaisesRegex(WorkspaceScanError, "unstable"):
                self.capture()

    def test_compare_rejects_incompatible_or_incomplete_objects(self) -> None:
        """若不同范围可比较，范围变化可能被伪装成 unchanged。"""

        baseline = self.capture()
        with self.assertRaises(ValueError):
            compare_baselines(baseline, replace(baseline, scope_version="other"))
        with self.assertRaises(ValueError):
            compare_baselines(baseline, replace(baseline, complete=False))
        with self.assertRaisesRegex(ValueError, "身份"):
            compare_baselines(
                baseline,
                replace(baseline, root_identity=(999, 999, 999)),
            )

    def test_snapshot_id_binds_root_identity_even_for_empty_workspace(self) -> None:
        """空目录被同路径替换后也必须是不同候选，不能按空清单判 unchanged。"""

        baseline = self.capture()
        self.workspace.rmdir()
        self.workspace.mkdir()
        replaced = self.capture()

        self.assertNotEqual(baseline.root_identity, replaced.root_identity)
        self.assertNotEqual(baseline.snapshot_id, replaced.snapshot_id)

    def test_filename_newlines_and_bidi_controls_cannot_inject_diff_headers(self) -> None:
        """文件名是终端结构，不得保留换行、回车或双向覆盖控制符。"""

        baseline = self.capture()
        malicious = "evil\n--- forged\r\u202egnp.txt"
        entry = FileSnapshotEntry(
            malicious,
            "text",
            4,
            "digest",
            0o644,
            (1, 2, 3),
            "safe",
        )
        after = replace(baseline, snapshot_id="candidate", entries=(entry,))

        preview = compare_baselines(baseline, after)

        self.assertNotIn(malicious, preview.full_diff)
        self.assertIn(r"evil\n--- forged\r\u202e", preview.full_diff)

    def test_untrusted_filename_and_content_controls_are_escaped(self) -> None:
        """若原始控制字符进入终端，diff 可伪造界面或隐藏确认内容。"""

        path = self.workspace / "evil\x1bname.txt"
        try:
            path.write_text("before\x1b[2J\n", encoding="utf-8")
        except OSError:
            self.skipTest("当前文件系统不允许控制字符文件名")
        before = self.capture()
        path.write_text("after\x1b[2J\n", encoding="utf-8")
        preview = compare_baselines(before, self.capture())

        self.assertNotIn("\x1b", preview.full_diff)
        self.assertIn("\\x1b", preview.full_diff)
