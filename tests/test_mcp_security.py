"""MCP stdio 服务启动前的安全边界测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from tricoder.mcp.security import (
    MCP_START_APPROVAL_ACTION,
    POSIX_SDK_DEFAULT_KEYS,
    WINDOWS_SDK_DEFAULT_KEYS,
    MCPLaunchError,
    MCPLaunchRequest,
    MCPStartRejected,
    approve_mcp_start,
    prepare_mcp_launch,
)
from tricoder.models import MCPServerConfig


class MCPLaunchSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.bin_dir = self.root / "bin"
        self.workspace.mkdir()
        self.bin_dir.mkdir()
        self.command_name = "mcp-test-server"
        executable_name = (
            f"{self.command_name}.exe" if os.name == "nt" else self.command_name
        )
        self.executable = self.bin_dir / executable_name
        self.executable.write_bytes(b"test executable")
        self.executable.chmod(0o755)
        self.source_env = {
            "PATH": str(self.bin_dir),
            "PATHEXT": ".EXE;.COM",
            "SystemRoot": r"C:\Windows",
            "SYSTEMROOT": r"C:\Windows",
            "SYSTEMDRIVE": "C:",
            "TEMP": str(self.root / "temp"),
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "USERPROFILE": r"C:\Users\private-user",
            "HOME": "/home/private-user",
            "USERNAME": "private-user",
            "USER": "private-user",
            "LOGNAME": "private-user",
            "SHELL": "/bin/sh",
            "TERM": "xterm-256color",
            "APPDATA": r"C:\Users\private-user\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\private-user\AppData\Local",
            "HOMEDRIVE": "C:",
            "HOMEPATH": r"\Users\private-user",
            "OPENAI_API_KEY": "provider-openai-secret",
            "DEEPSEEK_API_KEY": "provider-deepseek-secret",
            "DOCS_MCP_TOKEN": "docs-mcp-secret",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(
        self,
        *,
        command: str | None = None,
        args: tuple[str, ...] = (),
        enabled: bool = True,
        credentials_authorized: bool = True,
        credentials_present: bool = True,
        credential_env: tuple[str, ...] = ("DOCS_MCP_TOKEN",),
    ) -> MCPServerConfig:
        return MCPServerConfig(
            id="docs",
            transport="stdio",
            command=command or self.command_name,
            args=args,
            enabled=enabled,
            credential_env=credential_env,
            credentials_authorized=credentials_authorized,
            credentials_present=credentials_present,
        )

    def _prepare(
        self,
        config: MCPServerConfig | None = None,
        *,
        source_env: dict[str, str] | None = None,
    ) -> MCPLaunchRequest:
        return prepare_mcp_launch(
            config or self._config(),
            workspace=self.workspace,
            source_env=self.source_env if source_env is None else source_env,
        )

    def test_resolves_pure_executable_name_from_trusted_absolute_path(self) -> None:
        """若解析结果没有固定到可信绝对文件，子进程可能被 cwd/PATH 劫持。"""
        request = self._prepare()

        self.assertEqual(self.executable.resolve(), Path(request.command))
        self.assertEqual(self.workspace.resolve(), request.cwd)

    def test_rejects_qualified_windows_and_posix_commands(self) -> None:
        """command 带路径成分时会绕过受控 PATH 搜索，必须在启动前拒绝。"""
        for command in (r"C:\outside\server.exe", "./server", "../server", "/bin/server"):
            with self.subTest(command=command):
                with self.assertRaises(MCPLaunchError):
                    self._prepare(self._config(command=command))

    def test_ignores_empty_and_relative_path_entries(self) -> None:
        """空 PATH 项或相对目录不得从当前目录解析攻击者程序。"""
        relative_bin = self.root / "relative-bin"
        relative_bin.mkdir()
        relative_executable = relative_bin / self.executable.name
        relative_executable.write_bytes(b"relative executable")
        relative_executable.chmod(0o755)
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            source = dict(self.source_env)
            source["PATH"] = f"{os.pathsep}relative-bin"
            with self.assertRaises(MCPLaunchError):
                self._prepare(source_env=source)
        finally:
            os.chdir(old_cwd)

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_rejects_symlink_or_reparse_executable(self) -> None:
        """PATH 中的符号链接/reparse 可把纯名称重定向到未审批程序。"""
        linked_bin = self.root / "linked-bin"
        linked_bin.mkdir()
        link = linked_bin / self.executable.name
        try:
            link.symlink_to(self.executable)
        except OSError:
            self.skipTest("当前账户无权创建符号链接")
        source = dict(self.source_env)
        source["PATH"] = str(linked_bin)

        with self.assertRaises(MCPLaunchError):
            self._prepare(source_env=source)

    def test_python_uses_current_trusted_interpreter(self) -> None:
        """python 不得从项目 PATH 中选择同名伪程序。"""
        request = self._prepare(
            self._config(command="python", args=("-m", "docs_server"))
        )

        self.assertEqual(Path(sys.executable).resolve(), Path(request.command))

    def test_rejects_runtime_install_and_download_signatures(self) -> None:
        """启动 MCP 时动态安装依赖会扩大供应链与网络攻击面。"""
        cases = (
            ("npx", ("-y", "some-server")),
            ("npm", ("exec", "--yes", "some-server")),
            ("pip", ("install", "some-server")),
            ("python", ("-m", "pip", "install", "some-server")),
            ("uv", ("run", "--with", "some-server", "entrypoint")),
        )
        for command, args in cases:
            with self.subTest(command=command, args=args):
                with self.assertRaises(MCPLaunchError):
                    self._prepare(self._config(command=command, args=args))

    def test_rejects_install_signatures_with_global_option_variants(self) -> None:
        """安装器的全局选项和 --flag=value 不能绕过子命令识别。"""
        cases = (
            ("npx", ("--yes=true", "some-package")),
            ("npm", ("--yes", "exec", "some-package")),
            ("pip", ("--isolated", "install", "some-package")),
            ("python", ("-m", "pip", "--isolated", "install", "some-package")),
            ("uv", ("--quiet", "run", "--with", "some-package", "entry")),
        )
        for command, args in cases:
            with self.subTest(command=command, args=args):
                with self.assertRaises(MCPLaunchError) as raised:
                    self._prepare(self._config(command=command, args=args))

                self.assertEqual("mcp_runtime_install_rejected", str(raised.exception))

    def test_rejects_standalone_and_option_paths_outside_workspace(self) -> None:
        """独立路径与 --name=value 都必须经过同一 WorkspacePolicy。"""
        outside = self.root / "outside.json"
        for argument in (str(outside), f"--config={outside}", "../outside.json"):
            with self.subTest(argument=argument):
                with self.assertRaises(MCPLaunchError):
                    self._prepare(self._config(args=(argument,)))

    def test_rejects_compact_python_module_install_options(self) -> None:
        """紧凑 -m 和前置短选项不得绕过自动安装禁令。"""
        for args in (
            ("-mpip", "install", "some-package"),
            ("-I", "-mpip", "--isolated", "install", "some-package"),
            ("-Impip", "install", "some-package"),
            ("-BIm", "pip", "install", "some-package"),
            ("-W", "ignore", "-mpip", "install", "some-package"),
            ("-Xutf8", "-mpip", "install", "some-package"),
        ):
            with self.subTest(args=args):
                with self.assertRaisesRegex(MCPLaunchError, "^mcp_runtime_install_rejected$"):
                    self._prepare(self._config(command="python", args=args))

    def test_python_install_detection_stops_at_real_entrypoint(self) -> None:
        """脚本正文、模块参数和相似名称都不是 Python 的 pip 启动形式。"""
        for args in (
            ("-mpipeline", "install"),
            ("-mPIP", "install"),
            ("server.py", "-mpip", "install"),
            ("-m", "server", "-mpip", "install"),
            ("-mserver", "-m", "pip", "install"),
            ("-c", "print('safe')", "-mpip", "install"),
            ("--", "server.py", "-m", "pip", "install"),
            ("-W", "-mpip", "server.py", "install"),
        ):
            with self.subTest(args=args):
                try:
                    request = self._prepare(self._config(command="python", args=args))
                except MCPLaunchError as exc:
                    self.fail(f"非安装入口被误拒绝：{exc}")
                self.assertEqual(args, request.args)

    def test_accepts_path_like_arguments_inside_workspace(self) -> None:
        """工作区内路径仍可作为 server 配置或脚本参数。"""
        config_path = self.workspace / "config.json"
        config_path.write_text("{}", encoding="utf-8")

        request = self._prepare(
            self._config(args=("config.json", "--config=config.json"))
        )

        self.assertEqual(("config.json", "--config=config.json"), request.args)

    def test_rejects_nul_and_argv_resource_limit_violations(self) -> None:
        """argv 的项数、单项和总长度均需在创建进程前有硬上限。"""
        cases = (
            ("nul", ("bad\0value",)),
            ("count", tuple("x" for _ in range(65))),
            ("single", ("x" * 4097,)),
            ("total", tuple("x" * 4096 for _ in range(9))),
        )
        for label, args in cases:
            with self.subTest(label=label):
                with self.assertRaises(MCPLaunchError):
                    self._prepare(self._config(args=args))

    def test_total_argv_limit_includes_the_command(self) -> None:
        """总长度若漏算 executable，会让实际交给 OS 的 argv 越过硬上限。"""
        args = tuple("x" * 4094 for _ in range(8))
        self.assertLessEqual(sum(map(len, args)) + len(args) - 1, 32_768)

        with self.assertRaises(MCPLaunchError):
            self._prepare(self._config(args=args))

    def test_builds_explicit_minimal_environment_without_provider_secrets(self) -> None:
        """SDK 默认继承键必须全部覆盖，且只能加入本 server 已授权凭据。"""
        request = self._prepare()
        defaults = WINDOWS_SDK_DEFAULT_KEYS if os.name == "nt" else POSIX_SDK_DEFAULT_KEYS

        self.assertTrue(defaults.issubset(request.env))
        self.assertEqual("docs-mcp-secret", request.env["DOCS_MCP_TOKEN"])
        self.assertNotIn("OPENAI_API_KEY", request.env)
        self.assertNotIn("DEEPSEEK_API_KEY", request.env)
        if os.name == "nt":
            for name in (
                "APPDATA",
                "HOMEDRIVE",
                "HOMEPATH",
                "LOCALAPPDATA",
                "USERNAME",
                "USERPROFILE",
            ):
                self.assertEqual("", request.env[name])
            self.assertEqual(str(self.bin_dir), request.env["PATH"])
            self.assertEqual(r"C:\Windows", request.env["SYSTEMROOT"])
        else:
            for name in ("HOME", "LOGNAME", "USER"):
                self.assertEqual("", request.env[name])
            self.assertEqual(str(self.bin_dir), request.env["PATH"])
            self.assertEqual("/bin/sh", request.env["SHELL"])

    def test_rejects_disabled_unauthorized_missing_or_inconsistent_credentials(self) -> None:
        """配置状态与实际 source_env 任一不完整时都不能进入审批/进程阶段。"""
        cases = (
            self._config(enabled=False),
            self._config(credentials_authorized=False),
            self._config(credentials_present=False),
        )
        for config in cases:
            with self.subTest(config=config):
                with self.assertRaises(MCPLaunchError):
                    self._prepare(config)

        source = dict(self.source_env)
        del source["DOCS_MCP_TOKEN"]
        with self.assertRaises(MCPLaunchError):
            self._prepare(source_env=source)

    def test_never_reintroduces_provider_or_sdk_personal_keys_as_credentials(self) -> None:
        """误授权也不能把 Provider key 或 SDK 默认个人路径注入 server。"""
        personal_name = "USERPROFILE" if os.name == "nt" else "HOME"
        for name in ("OPENAI_API_KEY", personal_name):
            with self.subTest(name=name):
                with self.assertRaises(MCPLaunchError):
                    self._prepare(self._config(credential_env=(name,)))

    def test_rejects_personal_credential_names_from_both_platforms(self) -> None:
        """运行平台不能成为重新注入另一平台个人身份键的旁路。"""
        personal_names = (
            "APPDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "LOCALAPPDATA",
            "USERNAME",
            "USERPROFILE",
            "HOME",
            "LOGNAME",
            "USER",
        )
        for name in personal_names:
            with self.subTest(name=name):
                with self.assertRaises(MCPLaunchError) as raised:
                    self._prepare(self._config(credential_env=(name,)))

                self.assertEqual("mcp_credentials_unauthorized", str(raised.exception))

    def test_request_defensively_copies_and_freezes_environment(self) -> None:
        """调用方不得在审批后原地替换即将传给 server 的环境。"""
        source = dict(self.source_env)
        request = self._prepare(source_env=source)
        source["DOCS_MCP_TOKEN"] = "mutated"

        self.assertEqual("docs-mcp-secret", request.env["DOCS_MCP_TOKEN"])
        with self.assertRaises(TypeError):
            request.env["DOCS_MCP_TOKEN"] = "mutated"  # type: ignore[index]

    def test_approval_detail_contains_only_executable_and_bounded_args(self) -> None:
        """审批详情展示实际程序和参数，但不能拼接环境变量值。"""
        request = self._prepare(self._config(args=("serve",)))

        self.assertIn(str(self.executable.resolve()), request.approval_detail)
        self.assertIn("serve", request.approval_detail)
        for secret in (
            "provider-openai-secret",
            "provider-deepseek-secret",
            "docs-mcp-secret",
        ):
            self.assertNotIn(secret, request.approval_detail)

    def test_rejects_exact_or_embedded_credential_values_in_argv(self) -> None:
        """argv 不得把 server 或 Provider 凭据绕回审批详情。"""
        cases = (
            ("server-exact", "docs-mcp-secret"),
            ("provider-embedded", "--token=provider-openai-secret"),
        )
        for label, argument in cases:
            with self.subTest(label=label):
                with self.assertRaises(MCPLaunchError) as raised:
                    self._prepare(self._config(args=(argument,)))

                self.assertEqual("mcp_argv_contains_credential", str(raised.exception))
                self.assertNotIn("docs-mcp-secret", str(raised.exception))
                self.assertNotIn("provider-openai-secret", str(raised.exception))

    def test_rejects_server_credential_equal_to_resolved_executable(self) -> None:
        """最终 executable 与 server credential 完全相同时不得进入审批文本。"""
        executable_sentinel = str(self.executable.resolve())
        source = dict(self.source_env)
        source["DOCS_MCP_TOKEN"] = executable_sentinel

        with self.assertRaises(MCPLaunchError) as raised:
            self._prepare(source_env=source)

        self.assertEqual("mcp_argv_contains_credential", str(raised.exception))
        self.assertNotIn(executable_sentinel, str(raised.exception))

    def test_rejects_provider_credential_embedded_in_resolved_executable(self) -> None:
        """Provider credential 即使只出现在 executable 路径片段中也必须拒绝。"""
        provider_sentinel = "provider-path-sentinel"
        sentinel_bin = self.root / provider_sentinel
        sentinel_bin.mkdir()
        sentinel_executable = sentinel_bin / self.executable.name
        sentinel_executable.write_bytes(b"test executable")
        sentinel_executable.chmod(0o755)
        source = dict(self.source_env)
        source["PATH"] = str(sentinel_bin)
        source["OPENAI_API_KEY"] = provider_sentinel

        with self.assertRaises(MCPLaunchError) as raised:
            self._prepare(source_env=source)

        self.assertEqual("mcp_argv_contains_credential", str(raised.exception))
        self.assertNotIn(provider_sentinel, str(raised.exception))

    def test_approve_calls_human_once_and_uses_fixed_rejection(self) -> None:
        """拒绝启动时只产生一次审批，并返回不包含环境值的稳定错误类别。"""
        request = self._prepare(self._config(args=("serve",)))
        calls: list[tuple[str, str]] = []

        def reject(action: str, detail: str) -> bool:
            calls.append((action, detail))
            return False

        with self.assertRaises(MCPStartRejected) as raised:
            approve_mcp_start(request, "docs", reject)

        self.assertEqual(1, len(calls))
        self.assertEqual(MCP_START_APPROVAL_ACTION, calls[0][0])
        self.assertIn("docs", calls[0][1])
        self.assertNotIn("docs-mcp-secret", calls[0][1])
        self.assertEqual("mcp_start_rejected", str(raised.exception))

    def test_approve_returns_true_when_human_accepts(self) -> None:
        """启动权限最终由注入的人类审批器决定。"""
        request = self._prepare()
        calls = 0

        def accept(_action: str, _detail: str) -> bool:
            nonlocal calls
            calls += 1
            return True

        self.assertTrue(approve_mcp_start(request, "docs", accept))
        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
