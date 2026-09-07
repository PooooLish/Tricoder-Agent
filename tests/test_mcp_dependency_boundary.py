import builtins
import importlib
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tricoder.mcp.sdk import MCPDependencyError, load_mcp_sdk


class MCPDependencyBoundaryTests(unittest.TestCase):
    def test_missing_sdk_has_stable_safe_error(self) -> None:
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError("MCP-IMPORT-INTERNAL-SENTINEL")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked):
            with self.assertRaisesRegex(MCPDependencyError, "未安装 MCP SDK") as caught:
                load_mcp_sdk()
        self.assertNotIn("SENTINEL", str(caught.exception))

    def test_importing_tricoder_mcp_does_not_eagerly_import_sdk(self) -> None:
        real_import = builtins.__import__

        def guard(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "mcp" or name.startswith("mcp."):
                raise AssertionError("third-party SDK imported eagerly")
            return real_import(name, *args, **kwargs)

        package = importlib.import_module("tricoder.mcp")
        with patch("builtins.__import__", side_effect=guard):
            package = importlib.reload(package)
        self.assertIn("MCPDependencyError", package.__all__)

    def test_disabled_path_in_fresh_process_keeps_all_optional_packages_unloaded(self):
        script = """
import sys
import tricoder.cli
import tricoder.config
import tricoder.session_runtime
import tricoder.mcp
import tricoder.mcp.transport
from tricoder.mcp import MCPProcessExitEvidence, MCPTransportOutcome, VerifiedStdioTransport
from pathlib import Path
from tricoder.models import AppConfig, ProviderConfig
from tricoder.mcp.manager import MCPManager
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext
from tricoder.core.cancellation import CancellationToken
import asyncio
workspace = Path.cwd()
config = AppConfig(workspace=workspace, provider=ProviderConfig("openai", "offline-test", "https://example.test", "test"))
context = ToolContext(WorkspacePolicy(workspace), CommandPolicy(workspace), lambda *args: False)
async def disabled():
    manager = MCPManager(config, context, source_env={}, audit=None)
    await manager.start_all(CancellationToken())
    await manager.stop_all()
asyncio.run(disabled())
for prefix in ("mcp", "mcp_types", "anyio"):
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_missing_required_process_helper_fails_closed_before_spawn(self):
        utilities = importlib.import_module("mcp.os.win32.utilities")
        spawned = []

        async def forbidden_spawn(*args, **kwargs):
            spawned.append(True)
            raise AssertionError("不得创建进程")

        for invalid in (None, 17):
            with self.subTest(invalid=invalid):
                with patch.object(utilities, "terminate_windows_process_tree", invalid):
                    with patch.object(utilities, "create_windows_process", forbidden_spawn):
                        with self.assertRaises(MCPDependencyError) as caught:
                            load_mcp_sdk()
                self.assertEqual("已启用 MCP，但未安装 MCP SDK；请安装项目锁定依赖", str(caught.exception))
        self.assertEqual([], spawned)

    def test_sdk_version_drift_fails_closed(self):
        with patch("importlib.metadata.version", return_value="2.1.2"):
            with self.assertRaises(MCPDependencyError):
                load_mcp_sdk()

    def test_loader_exposes_verified_bindings_and_exact_log_source_identities(self):
        sdk = load_mcp_sdk()
        self.assertTrue(callable(sdk.stdio_bindings.create_process))
        sources = {(source.logger_name, source.source_path) for source in sdk.log_sources}
        session = importlib.import_module("mcp.client.session")
        self.assertIn(("client", os.path.normcase(os.path.abspath(session.__file__))), sources)
        for source in sdk.log_sources:
            self.assertTrue(Path(source.source_path).is_file())
            self.assertIsInstance(logging.getLogger(source.logger_name), logging.Logger)

    def test_parser_returns_message_or_exception_without_logging(self):
        sdk = load_mcp_sdk()
        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(record)

        capture = Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            parsed = sdk.stdio_bindings.parse_message('{"jsonrpc":"2.0","id":1,"result":{}}')
            rejected = sdk.stdio_bindings.parse_message("not-json-parser-sentinel")
        finally:
            root.removeHandler(capture)
        self.assertEqual(1, parsed.message.id)
        self.assertIsInstance(rejected, Exception)
        self.assertEqual([], messages)

    def test_missing_required_log_identity_fails_closed_before_spawn(self):
        for module_name in (
            "mcp.client.session", "mcp.client.stdio", "mcp.shared.jsonrpc_dispatcher",
            "mcp.shared.dispatcher",
            "mcp.os.win32.utilities" if sys.platform == "win32" else "mcp.os.posix.utilities",
        ):
            module = importlib.import_module(module_name)
            for pathname in (None, "relative/session.py"):
                with self.subTest(module=module_name, pathname=pathname):
                    with patch.object(module, "__file__", pathname):
                        with self.assertRaises(MCPDependencyError):
                            load_mcp_sdk()

    def test_log_source_discovery_is_lexical_and_includes_shared_dispatcher(self):
        dispatcher = importlib.import_module("mcp.shared.dispatcher")
        # 以已导入模块身份为边界，不检查文件系统中的实体是否仍存在。
        with patch("pathlib.Path.resolve", side_effect=AssertionError("no filesystem resolution")):
            sdk = load_mcp_sdk()
        self.assertIn(
            ("mcp.shared.dispatcher", os.path.normcase(os.path.abspath(dispatcher.__file__))),
            {(source.logger_name, source.source_path) for source in sdk.log_sources},
        )

    def test_inactive_platform_log_source_may_be_absent(self):
        module_name = "mcp.os.posix.utilities" if sys.platform == "win32" else "mcp.os.win32.utilities"
        module = importlib.import_module(module_name)
        with patch.object(module, "__file__", None):
            sdk = load_mcp_sdk()
        self.assertNotIn(module_name, {source.logger_name for source in sdk.log_sources})


class MCPProcessBindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_posix_spawn_uses_only_approved_environment_and_new_session(self):
        anyio = importlib.import_module("anyio")
        captured = {}
        process = SimpleNamespace(pid=123, returncode=None, stdin=object(), stdout=object())

        async def capture_spawn(command, **kwargs):
            captured.update(command=command, **kwargs)
            return process

        with patch.object(anyio, "open_process", capture_spawn):
            with patch("tricoder.mcp.sdk.sys", SimpleNamespace(platform="linux"), create=True):
                sdk = load_mcp_sdk()
        parameters = SimpleNamespace(command="approved-program", args=["approved-argument"], env={"ONLY": "approved"}, cwd="approved-cwd")
        stderr = io.StringIO()
        result = await sdk.stdio_bindings.create_process(parameters, stderr)
        self.assertIs(process, result)
        self.assertEqual({
            "command": ["approved-program", "approved-argument"],
            "env": {"ONLY": "approved"},
            "cwd": "approved-cwd",
            "stderr": stderr,
            "start_new_session": True,
        }, captured)

    async def test_windows_spawn_uses_job_aware_creator_and_exact_approved_parameters(self):
        utilities = importlib.import_module("mcp.os.win32.utilities")
        captured = {}
        process = SimpleNamespace(pid=123, returncode=None, stdin=object(), stdout=object())

        async def capture_spawn(command, args, **kwargs):
            captured.update(command=command, args=args, **kwargs)
            return process

        with patch.object(utilities, "create_windows_process", capture_spawn):
            with patch("tricoder.mcp.sdk.sys", SimpleNamespace(platform="win32"), create=True):
                sdk = load_mcp_sdk()
        parameters = SimpleNamespace(command="approved.exe", args=["argument"], env={"ONLY": "approved"}, cwd="approved-cwd")
        stderr = io.StringIO()
        result = await sdk.stdio_bindings.create_process(parameters, stderr)
        self.assertIs(process, result)
        self.assertEqual({
            "command": "approved.exe", "args": ["argument"], "env": {"ONLY": "approved"},
            "cwd": "approved-cwd", "errlog": stderr,
        }, captured)
