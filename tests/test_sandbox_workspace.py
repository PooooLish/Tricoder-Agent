"""Docker 模式独立工作副本与基线差异测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.models import SandboxConfig
from tricoder.sandbox.execution import build_command_policy
from tricoder.sandbox.workspace import (
    CopyBudgets,
    SandboxWorkspace,
    SandboxWorkspaceError,
)


class SandboxWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original = self.root / "project"
        self.runtime = self.root / "state" / "docker-sandbox"
        self.original.mkdir()
        (self.original / "src").mkdir()
        (self.original / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.original / "tests").mkdir()
        (self.original / "tests" / "test_app.py").write_text(
            "import unittest\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare(self, session_id: str = "session-a", generation: int = 0) -> SandboxWorkspace:
        return SandboxWorkspace.prepare(
            self.original,
            self.runtime,
            session_id=session_id,
            generation=generation,
        )

    def test_copy_includes_uncommitted_source_but_never_reads_sensitive_or_generated_paths(self) -> None:
        """破坏点：依赖 Git 或先读后滤会漏掉未提交源码并暴露凭据。"""
        (self.original / ".env.local").write_text("SECRET-SENTINEL", encoding="utf-8")
        (self.original / ".hidden-source").write_text("kept\n", encoding="utf-8")
        (self.original / ".git").mkdir()
        (self.original / ".git" / "config").write_text("private", encoding="utf-8")
        (self.original / ".venv").mkdir()
        (self.original / ".venv" / "marker").write_text("private", encoding="utf-8")
        (self.original / "runtime").mkdir()
        (self.original / "runtime" / "old.log").write_text("private", encoding="utf-8")

        sandbox = self._prepare()

        self.assertEqual("value = 1\n", (sandbox.execution_workspace / "src" / "app.py").read_text("utf-8"))
        self.assertTrue((sandbox.execution_workspace / ".hidden-source").is_file())
        self.assertFalse((sandbox.execution_workspace / ".env.local").exists())
        self.assertFalse((sandbox.execution_workspace / ".git").exists())
        self.assertFalse((sandbox.execution_workspace / ".venv").exists())
        self.assertFalse((sandbox.execution_workspace / "runtime").exists())
        self.assertIn(".env.local", sandbox.excluded_paths)
        self.assertTrue(sandbox.control_path.is_file())
        self.assertFalse(sandbox.control_path.is_relative_to(sandbox.execution_workspace))

    def test_two_sessions_have_distinct_copies_and_original_remains_unchanged(self) -> None:
        """破坏点：复用副本会串写 Session，或直接修改原项目。"""
        first = self._prepare("first")
        second = self._prepare("second")

        (first.execution_workspace / "src" / "app.py").write_text(
            "value = 2\n", encoding="utf-8"
        )

        self.assertNotEqual(first.execution_workspace, second.execution_workspace)
        self.assertEqual("value = 1\n", (second.execution_workspace / "src" / "app.py").read_text("utf-8"))
        self.assertEqual("value = 1\n", (self.original / "src" / "app.py").read_text("utf-8"))

    def test_baseline_diff_reports_modified_created_and_deleted_paths_without_git(self) -> None:
        """破坏点：没有复制 .git 时仍调用 git 会失败或越界读取原仓库。"""
        sandbox = self._prepare()
        (sandbox.execution_workspace / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (sandbox.execution_workspace / "new.py").write_text("created = True\n", encoding="utf-8")
        (sandbox.execution_workspace / "tests" / "test_app.py").unlink()

        stat = sandbox.diff_stat()

        self.assertIn("M src/app.py", stat)
        self.assertIn("A new.py", stat)
        self.assertIn("D tests/test_app.py", stat)
        self.assertNotIn(str(self.original), stat)

    def test_copy_rejects_symlink_and_hardlink_entries(self) -> None:
        """破坏点：跟随链接可把工作区外内容复制进容器。"""
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        symlink = self.original / "link.txt"
        try:
            symlink.symlink_to(outside)
        except OSError:
            self.skipTest("当前平台无创建 symlink 权限")
        with self.assertRaises(SandboxWorkspaceError):
            self._prepare("symlink")

        symlink.unlink()
        hardlink = self.original / "hardlink.py"
        try:
            os.link(outside, hardlink)
        except OSError:
            self.skipTest("当前文件系统不支持 hardlink")
        with self.assertRaises(SandboxWorkspaceError):
            self._prepare("hardlink")

    @unittest.skipIf(os.name == "nt", "Windows 不支持 mkfifo")
    def test_copy_rejects_special_files(self) -> None:
        """破坏点：FIFO/设备等特殊文件会阻塞复制或扩大容器能力。"""
        fifo = self.original / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(SandboxWorkspaceError):
            self._prepare("special")

    def test_budget_failure_and_precancel_remove_partial_generation(self) -> None:
        """破坏点：超限或取消后残留半成品可能被后续会话误激活。"""
        (self.original / "large.txt").write_text("x" * 20, encoding="utf-8")
        with self.assertRaises(SandboxWorkspaceError):
            SandboxWorkspace.prepare(
                self.original,
                self.runtime,
                session_id="budget",
                generation=0,
                budgets=CopyBudgets(max_entries=20, max_file_bytes=10, max_total_bytes=100),
            )
        self.assertFalse((self.runtime / "budget" / "0").exists())

        token = CancellationToken()
        token.cancel()
        with self.assertRaises(CancellationError):
            SandboxWorkspace.prepare(
                self.original,
                self.runtime,
                session_id="cancelled",
                generation=0,
                cancellation=token,
            )
        self.assertFalse((self.runtime / "cancelled" / "0").exists())

    def test_existing_complete_copy_resumes_but_incomplete_directory_is_rejected(self) -> None:
        """破坏点：重启若覆盖草稿会丢工作，若接受半成品会错认完整基线。"""
        first = self._prepare("resume")
        (first.execution_workspace / "src" / "app.py").write_text("draft = True\n", encoding="utf-8")

        resumed = self._prepare("resume")

        self.assertTrue(resumed.resumed)
        self.assertEqual("draft = True\n", (resumed.execution_workspace / "src" / "app.py").read_text("utf-8"))

        incomplete = self.runtime / "incomplete" / "0"
        incomplete.mkdir(parents=True)
        with self.assertRaises(SandboxWorkspaceError):
            self._prepare("incomplete")

    def test_docker_command_policy_rejects_git_history_and_status_processes(self) -> None:
        """破坏点：副本没有 .git，任何 git 子进程都可能向上读取外部仓库。"""
        sandbox = self._prepare("policy")
        policy = build_command_policy(
            SandboxConfig(mode="docker", image="python@sha256:" + "6" * 64),
            sandbox.execution_workspace,
        )

        for command in ("git status", "git show HEAD:.env.local", "git log -n 1"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                policy.validate(command)


if __name__ == "__main__":
    unittest.main()
