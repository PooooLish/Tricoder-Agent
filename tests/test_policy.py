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
            "python -m pytest -q",
            "python -m ruff check src",
            "python -m mypy src",
            "git status --short",
            "git diff",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.policy.validate(command))

    def test_validate_resolves_trusted_executables(self) -> None:
        """校验通过后 args[0] 必须是 PATH 解析出的绝对路径，执行来源可信。"""
        for command, name in (
            ("python -m unittest", "python"),
            ("git status", "git"),
        ):
            with self.subTest(command=command):
                args = self.policy.validate(command)
                resolved = Path(args[0])
                self.assertTrue(resolved.is_absolute())
                self.assertEqual(name, resolved.name.lower().removesuffix(".exe"))
                self.assertTrue(resolved.exists())

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

    def test_accepts_safe_verification_command_arguments(self) -> None:
        """确保收紧后常用安全参数仍可正常使用。"""
        commands = (
            "python -m pytest -q -x --maxfail=3 -k test_api",
            "python -m pytest tests/test_api.py",
            "python -m pytest tests",
            "python -m ruff check --select F401 src",
            "python -m ruff check src/app.py",
            "python -m ruff check src",
            "python -m mypy --ignore-missing-imports src",
            "python -m mypy src",
            "git diff --stat",
            "git status --short",
            "git log --oneline -5",
            "git show HEAD",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.policy.validate(command))

    def test_rejects_qualified_executable_paths(self) -> None:
        """可执行程序参数带路径会绕过白名单，必须拒绝。"""
        commands = (
            "C:\\outside\\pytest.exe -q",
            "C:\\outside\\python.exe -m unittest",
            "C:\\outside\\git.exe status",
            ".\\pytest -q",
            "..\\ruff check src",
            "./python -m unittest",
            "bin/python -m unittest",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_direct_pytest_ruff_mypy_calls(self) -> None:
        """pytest/ruff/mypy 只能通过 python -m 运行，防止 cwd 同名程序劫持。"""
        commands = (
            "pytest -q",
            "pytest tests",
            "ruff check src",
            "mypy src",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(PolicyError, "python -m"):
                    self.policy.validate(command)

    def test_rejects_option_values_that_escape_workspace(self) -> None:
        """`--option=value` 的值可能是外部路径或越界片段，必须拒绝。"""
        commands = (
            "python -m pytest --junitxml=C:\\outside\\result.xml",
            "python -m pytest --ignore=..\\outside",
            "python -m ruff check --cache-dir=C:\\outside src",
            "python -m mypy --cache-dir=C:\\outside src",
            "python -m pytest --rootdir=C:\\outside",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_accepts_workspace_script_execution(self) -> None:
        """python 运行工作区内相对 .py 脚本放行；审批级别控制自动执行。"""
        commands = (
            "python test/smoke_demo.py",
            "python test/smoke_demo.py -q",
            "python src/main.py --debug out.txt",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.policy.validate(command))

    def test_rejects_unsafe_script_execution(self) -> None:
        """脚本执行禁止绝对路径、越界、非 .py、裸选项或敏感路径段。"""
        commands = (
            "python C:\\outside\\script.py",
            "python ..\\outside.py",
            "python script",
            "python --version",
            "python .env/evil.py",
            "python .git/hooks/pre-commit.py",
            "python secrets/leak.py",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_git_compact_and_pager_options(self) -> None:
        """git 紧凑全局选项、pager 与 textconv 可切换目录或执行外部程序。"""
        commands = (
            "git -C..\\outside status",
            "git -Coutside status",
            "git -ccore.pager=calc --paginate log",
            "git -cfoo=bar status",
            "git --paginate log",
            "git diff --textconv",
            "git diff --no-index C:\\outside\\a C:\\outside\\b",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_accepts_git_no_pager_and_safe_flags(self) -> None:
        """git 的 --no-pager 等安全全局选项仍可使用。"""
        commands = (
            "git --no-pager status",
            "git --no-pager diff --stat",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.policy.validate(command))

    def test_rejects_pytest_plugin_and_external_code_loading(self) -> None:
        """pytest 的插件、配置和 pyargs 可加载或执行工作区外代码，必须拒绝。"""
        commands = (
            "pytest -p xdist",
            "pytest --plugins=dotenv",
            "pytest --pdb",
            "pytest -c custom.ini",
            "pytest --confcutdir C:\\outside",
            "pytest --pyargs external_package",
            "pytest --rootdir C:\\outside",
            "pytest --basetemp C:\\outside",
            "pytest -o addopts=--plugins=x",
            "python -m pytest -p xdist",
            "python -m pytest --override-ini=addopts=-p",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_test_tools_with_absolute_or_escaping_paths(self) -> None:
        """位置参数越界会让测试工具读取工作区外文件。"""
        commands = (
            "pytest C:\\outside\\test_x.py",
            "pytest ../outside",
            "ruff check C:\\outside\\app.py",
            "mypy C:\\outside\\app.py",
            "mypy ../outside",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_ruff_subcommands_and_write_options(self) -> None:
        """ruff 只做只读检查：禁 format 与任何改写文件的选项。"""
        commands = (
            "ruff format src",
            "ruff linter",
            "ruff check --fix src",
            "ruff check --add-noqa src",
            "ruff check --output-file C:\\outside\\report.txt src",
            "ruff check --stdin-filename src/app.py",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_mypy_external_loading_options(self) -> None:
        """mypy 的配置/模块/解释器选项可指向外部代码或工具。"""
        commands = (
            "mypy -c 'x = 1'",
            "mypy --command 'x = 1'",
            "mypy -m external_package",
            "mypy -p external_package",
            "mypy --config-file C:\\outside\\mypy.ini",
            "mypy --install-types",
            "mypy --python-executable C:\\outside\\python.exe",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_git_options_that_escape_workspace_or_execute_code(self) -> None:
        """git 的 -C/-c/--git-dir/--no-index 等可越界读文件或执行外部程序。"""
        commands = (
            "git -C C:\\outside status",
            "git -c core.pager=evil status",
            "git --git-dir=C:\\outside\\repo status",
            "git --work-tree=C:\\outside status",
            "git diff --no-index C:\\outside\\a C:\\outside\\b",
            "git diff --ext-diff",
            "git log --ext-diff",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)


class CommandPolicyWorkspaceTests(unittest.TestCase):
    """带 workspace 的 CommandPolicy：所有路径参数经 WorkspacePolicy 真实解析。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "test").mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "test" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "src" / "app.py").write_text("y = 2\n", encoding="utf-8")
        self.policy = CommandPolicy(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rejects_absolute_paths_in_tool_commands(self) -> None:
        """unittest/compileall/pytest/ruff/mypy 的路径参数必须真实位于工作区内。"""
        commands = (
            "python -m compileall C:\\outside",
            "python -m unittest discover -s C:\\outside",
            "python -m pytest C:\\outside",
            "python -m mypy C:\\outside",
            "python -m ruff check C:\\outside",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_indirect_compileall_and_unittest_boundaries(self) -> None:
        """禁止从清单读取路径，也禁止 unittest 通过模块名导入任意代码。"""
        commands = (
            "python -m compileall -i test/paths.txt",
            "python -m unittest xml.etree.ElementTree",
            "python -m unittest tests.test_example",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_git_output_redirect(self) -> None:
        """git 只读命令禁止 --output 等写文件选项。"""
        commands = (
            "git diff --output=C:\\outside\\leak.txt",
            "git log --output C:\\outside\\leak.txt",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_rejects_scripts_outside_workspace(self) -> None:
        """脚本必须解析为工作区内存在的普通 .py 文件。"""
        commands = (
            "python C:\\outside\\script.py",
            "python ..\\outside.py",
            "python test/missing.py",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_rejects_script_through_symlink_escape(self) -> None:
        """符号链接指向工作区外时，脚本路径必须被拒绝。"""
        outside = Path(tempfile.mkdtemp(prefix="script-outside-"))
        (outside / "outside.py").write_text("x = 1\n", encoding="utf-8")
        link = self.workspace / "test" / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("当前账户不能创建符号链接")
        try:
            with self.assertRaises(PolicyError):
                self.policy.validate("python test/link/outside.py")
        finally:
            link.unlink(missing_ok=True)
            outside.rmdir()

    def test_accepts_in_workspace_paths(self) -> None:
        """工作区内路径参数正常放行。"""
        commands = (
            "python -m compileall src",
            "python -m unittest discover -s test",
            "python -m unittest test/ok.py",
            "python test/ok.py",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.policy.validate(command))

    def test_audit_metadata_script_does_not_crash(self) -> None:
        """脚本命令的审计元数据必须结构化且不崩溃（原 IndexError）。"""
        meta = self.policy.audit_metadata("python test/ok.py")
        self.assertTrue(meta["command_valid"])
        self.assertEqual("script", meta["execution_kind"])
        self.assertEqual("test/ok.py", meta["script"])
        module_meta = self.policy.audit_metadata("python -m unittest")
        self.assertEqual("module", module_meta["execution_kind"])
        self.assertEqual("unittest", module_meta["python_module"])
        invalid = self.policy.audit_metadata("python C:\\outside\\script.py")
        self.assertFalse(invalid["command_valid"])


if __name__ == "__main__":
    unittest.main()
