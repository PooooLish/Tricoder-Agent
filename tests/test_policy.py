import os
import tempfile
import unittest
from pathlib import Path

from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        self.policy = WorkspacePolicy(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_accepts_normal_file_inside_workspace(self) -> None:
        """防止正常的相对代码路径被安全策略误伤。"""
        self.assertEqual(
            (self.workspace / "src" / "app.py").resolve(),
            self.policy.resolve_path("src/app.py"),
        )

    def test_rejects_parent_escape_and_outside_absolute_path(self) -> None:
        """防止模型通过相对或绝对路径读取工作区外文件。"""
        outside = self.workspace.parent / "outside.txt"
        for candidate in ("../outside.txt", str(outside)):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(PolicyError, "工作区"):
                    self.policy.resolve_path(candidate, must_exist=False)

    def test_rejects_sensitive_paths_but_allows_example_file(self) -> None:
        """防止凭据文件被读取，同时保留可提交的占位模板。"""
        for candidate in (".git/config", ".env", ".env.local", "keys/private.pem"):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(PolicyError, "敏感"):
                    self.policy.resolve_path(candidate, must_exist=False)
        allowed = self.policy.resolve_path(".env.example", must_exist=False)
        self.assertEqual((self.workspace / ".env.example").resolve(), allowed)

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_rejects_symlink_that_points_outside(self) -> None:
        """防止符号链接绕过工作区边界。"""
        outside_dir = self.workspace.parent / f"{self.workspace.name}-outside"
        outside_dir.mkdir(exist_ok=True)
        link = self.workspace / "external-link"
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            self.skipTest("当前账户不能创建符号链接")
        try:
            with self.assertRaisesRegex(PolicyError, "工作区"):
                self.policy.resolve_path("external-link/secret.txt", must_exist=False)
        finally:
            link.unlink(missing_ok=True)
            outside_dir.rmdir()


class CommandPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CommandPolicy()

    def test_accepts_only_verification_commands(self) -> None:
        """防止允许列表遗漏常用测试和只读 Git 检查。"""
        commands = (
            "python -m unittest",
            "python -m compileall src",
            "pytest -q",
            "ruff check src",
            "mypy src",
            "git status --short",
            "git diff",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.policy.validate(command))

    def test_rejects_shell_chaining_deletion_install_and_git_writes(self) -> None:
        """防止审批机制被高风险命令或 Shell 元字符绕过。"""
        commands = (
            "python -m unittest && del important.txt",
            "pytest | more",
            "rm -rf src",
            "pip install requests",
            "python -m pip install requests",
            "git commit -am unsafe",
            "git reset --hard",
            "git clean -fd",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_unlisted_executable(self) -> None:
        """防止模型借助任意程序越过有限的检查命令范围。"""
        with self.assertRaisesRegex(PolicyError, "允许"):
            self.policy.validate("powershell Get-ChildItem")


if __name__ == "__main__":
    unittest.main()
