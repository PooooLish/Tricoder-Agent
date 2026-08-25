import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tricoder.evals.models import EvalCase, VerificationSpec
from tricoder.evals.workspace import (
    RESERVED_VERIFIER_DIR,
    WorkspaceSafetyError,
    capture_snapshot,
    changed_paths,
    install_verifier,
    prepare_workspace,
    remove_verifier,
)


class EvalWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temp.name)
        self.fixture = self.run_root / "fixture"
        self.verifier_source = self.run_root / "verifier"
        self.fixture.mkdir()
        self.verifier_source.mkdir()
        (self.fixture / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.verifier_source / "test_hidden.py").write_text(
            "assert value == 2\n", encoding="utf-8"
        )
        self.case = EvalCase(
            id="fix-value",
            title="Fix value",
            task="Set value to 2.",
            source_dir=self.run_root,
            workspace_dir=self.fixture,
            verifier_dir=self.verifier_source,
            allowed_changes=("app.py",),
            required_changes=("app.py",),
            max_rounds=1,
            max_context_chars=100,
            verifications=(VerificationSpec("hidden", "python -m unittest -q", 30),),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_workspace_copies_fixture_without_verifier(self) -> None:
        """防止 Agent 在原 fixture 上修改或提前看到隐藏 verifier。"""
        workspace = prepare_workspace(self.case, self.run_root / "workspaces")

        self.assertEqual("value = 1\n", (workspace / "app.py").read_text("utf-8"))
        self.assertFalse((workspace / RESERVED_VERIFIER_DIR).exists())
        self.assertFalse((workspace / "hidden_test.py").exists())
        self.assertEqual("value = 1\n", (self.fixture / "app.py").read_text("utf-8"))

    def test_prepare_workspace_rejects_roots_overlapping_fixture_or_verifier(self) -> None:
        """防止工作副本根目录写入 fixture 或 verifier 源目录。"""
        nested_fixture = self.fixture / "nested-fixture"
        nested_fixture.mkdir()
        (nested_fixture / "app.py").write_text("value = 1\n", encoding="utf-8")
        nested_case = replace(self.case, id="nested-case", workspace_dir=nested_fixture)

        with self.assertRaisesRegex(WorkspaceSafetyError, "重叠|隔离"):
            prepare_workspace(nested_case, self.fixture)
        with self.assertRaisesRegex(WorkspaceSafetyError, "重叠|隔离"):
            prepare_workspace(self.case, self.verifier_source)

        self.assertFalse((self.fixture / "nested-case").exists())
        self.assertFalse((self.verifier_source / self.case.id).exists())

    def test_prepare_workspace_rejects_reserved_verifier_in_fixture(self) -> None:
        """防止畸形 fixture 在隐藏验证注入前泄露 verifier 内容。"""
        reserved = self.fixture / RESERVED_VERIFIER_DIR
        reserved.mkdir()
        (reserved / "test_hidden.py").write_text("raise AssertionError\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceSafetyError, "保留"):
            prepare_workspace(self.case, self.run_root / "workspaces")

        self.assertFalse((self.run_root / "workspaces" / self.case.id).exists())

    def test_verifier_is_installed_after_snapshot_and_removed(self) -> None:
        """注入前的 after 快照固定 Agent 修改，后续 verifier 不参与该差异。"""
        workspace = prepare_workspace(self.case, self.run_root / "workspaces")
        before = capture_snapshot(workspace)
        (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        after = capture_snapshot(workspace)

        verifier = install_verifier(self.case, workspace)

        self.assertTrue((verifier / "test_hidden.py").is_file())
        self.assertEqual(("app.py",), changed_paths(before, after))
        self.assertIn(
            f"{RESERVED_VERIFIER_DIR}/test_hidden.py",
            capture_snapshot(workspace),
        )
        remove_verifier(workspace)
        self.assertFalse(verifier.exists())

    def test_capture_snapshot_includes_agent_created_nested_reserved_path(self) -> None:
        """防止任意层级的保留目录名从 Agent 阶段修改集合中消失。"""
        workspace = prepare_workspace(self.case, self.run_root / "workspaces")
        before = capture_snapshot(workspace)
        reserved = workspace / "nested" / RESERVED_VERIFIER_DIR
        reserved.mkdir(parents=True)
        (reserved / "forged.py").write_text("pass\n", encoding="utf-8")

        after = capture_snapshot(workspace)

        self.assertIn(
            f"nested/{RESERVED_VERIFIER_DIR}/forged.py",
            after,
        )
        self.assertEqual(
            (f"nested/{RESERVED_VERIFIER_DIR}/forged.py",),
            changed_paths(before, after),
        )

    def test_changed_paths_reports_added_deleted_and_modified_paths_in_order(self) -> None:
        """防止快照遗漏新增、删除或内容变化的普通文件。"""
        workspace = prepare_workspace(self.case, self.run_root / "workspaces")
        before = capture_snapshot(workspace)
        (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        (workspace / "added.py").write_text("added\n", encoding="utf-8")
        after = capture_snapshot(workspace)
        (workspace / "added.py").unlink()
        (workspace / "removed.py").write_text("removed\n", encoding="utf-8")
        later = capture_snapshot(workspace)

        self.assertEqual(("added.py", "app.py"), changed_paths(before, after))
        self.assertEqual(("added.py", "removed.py"), changed_paths(after, later))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "当前平台不支持符号链接")
    def test_prepare_workspace_rejects_symlinked_fixture_entry(self) -> None:
        """防止递归复制跟随 fixture 内的符号链接离开评测目录。"""
        outside = self.run_root / "outside.py"
        outside.write_text("private = True\n", encoding="utf-8")
        try:
            (self.fixture / "escape.py").symlink_to(outside)
        except OSError:
            self.skipTest("当前账户不能创建符号链接")

        with self.assertRaisesRegex(WorkspaceSafetyError, "链接|reparse"):
            prepare_workspace(self.case, self.run_root / "workspaces")

    @unittest.skipUnless(os.name == "nt", "仅 Windows 支持 junction reparse point")
    def test_prepare_workspace_rejects_junction_fixture_entry(self) -> None:
        """防止 Windows junction 在递归复制时逃离 fixture 目录。"""
        outside = self.run_root / "outside"
        outside.mkdir()
        junction = self.fixture / "escape"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("当前账户不能创建 junction")

        with self.assertRaisesRegex(WorkspaceSafetyError, "链接|reparse"):
            prepare_workspace(self.case, self.run_root / "workspaces")


if __name__ == "__main__":
    unittest.main()
