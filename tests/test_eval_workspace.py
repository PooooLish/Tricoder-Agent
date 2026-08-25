import os
import subprocess
import tempfile
import unittest
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

    def test_verifier_is_installed_after_snapshot_and_removed(self) -> None:
        """防止隐藏 verifier 被快照误报为 Agent 的修改。"""
        workspace = prepare_workspace(self.case, self.run_root / "workspaces")
        before = capture_snapshot(workspace)
        (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        after = capture_snapshot(workspace)

        verifier = install_verifier(self.case, workspace)

        self.assertTrue((verifier / "test_hidden.py").is_file())
        self.assertEqual(("app.py",), changed_paths(before, after))
        self.assertEqual(after, capture_snapshot(workspace))
        remove_verifier(workspace)
        self.assertFalse(verifier.exists())

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
