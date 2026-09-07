"""真实、本地 MCP stdio 集成边界。"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.audit import AuditLogger
from tricoder.context import ToolResultSpillStore
from tricoder.core.cancellation import CancellationToken
from tricoder.mcp.client import (
    MCPClient,
    MCPProtocolError,
    MCPTimeoutError,
    _wait_for_cancellation,
)
from tricoder.mcp.manager import MCPManager
from tricoder.mcp.models import MCPServerState
from tricoder.mcp.runtime import run_mcp_task
from tricoder.mcp.security import prepare_mcp_launch
from tricoder.mcp.sdk import load_mcp_sdk
from tricoder.mcp.transport import MCPProcessExitEvidence, VerifiedStdioTransport
from tricoder.models import (
    AppConfig,
    ExtensionsConfig,
    MCPConfig,
    MCPServerConfig,
    ProviderConfig,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry


class MCPStdioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """不经 mock 的 repository-local stdio server 证明。"""

    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]
        self.fixture_arg = "tests/fixtures/fake_mcp_server.py"
        self.approvals: list[tuple[str, str]] = []
        self.temp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp.name).resolve() / "runtime"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approve(self, action: str, detail: str) -> bool:
        self.approvals.append((action, detail))
        return True

    def _source_env(self) -> dict[str, str]:
        """只构造启动 Python 所需的最小非秘密环境。"""

        return {
            "PATH": r"C:\\Windows\\System32",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SYSTEMROOT": r"C:\\Windows",
            "MCP_STATIC_ENV_SENTINEL": "mcp-static-env-sentinel",
        }

    def _config(self, *mode_args: str) -> AppConfig:
        return AppConfig(
            workspace=self.project_root,
            provider=ProviderConfig("openai", "test-key", "https://example.test", "test"),
            extensions=ExtensionsConfig(enabled=True),
            mcp=MCPConfig(
                enabled=True,
                servers=(
                    MCPServerConfig(
                        "local_test",
                        "stdio",
                        "python",
                        args=(self.fixture_arg, *mode_args),
                        enabled=True,
                    ),
                ),
            ),
        )

    def _registry(self, *, max_output_chars: int = 20_000) -> ToolRegistry:
        return ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.project_root),
                CommandPolicy(self.project_root),
                self._approve,
                max_output_chars=max_output_chars,
                spill_store=ToolResultSpillStore(self.runtime_root, "mcp-integration"),
            )
        )

    def _launch_request(self, *mode_args: str):  # type: ignore[no-untyped-def]
        return prepare_mcp_launch(
            MCPServerConfig(
                "local_test",
                "stdio",
                "python",
                args=(self.fixture_arg, *mode_args),
                enabled=True,
            ),
            workspace=self.project_root,
            source_env=self._source_env(),
        )

    async def test_local_stdio_echo_uses_registry_approvals_and_releases_mcp_tasks(self) -> None:
        """若 fixture、真实 transport、审批或 task-scope 任一环断开，此契约必须失败。"""

        await self._check_recorded_stdio_echo()

    async def test_task_recorder_restores_factory_when_interference_construction_fails(self) -> None:
        """若 try 边界晚于 launch/client 构造，失败会污染当前 loop factory。"""

        loop = asyncio.get_running_loop()
        previous_task_factory = loop.get_task_factory()
        for stage, expected_error in (("launch_request", RuntimeError), ("client", TypeError)):
            with self.subTest(stage=stage):
                observed_factories = []

                def reject_construction():  # type: ignore[no-untyped-def]
                    observed_factories.append(loop.get_task_factory())
                    if stage == "launch_request":
                        raise RuntimeError("controlled launch construction failure")
                    # 真实 MCPClient 构造器拒绝此值，尚无 coroutine/server 启动。
                    return object()

                try:
                    with patch.object(self, "_launch_request", side_effect=reject_construction):
                        with self.assertRaises(expected_error):
                            await self._check_recorded_stdio_echo()
                    self.assertEqual(1, len(observed_factories))
                    self.assertIsNot(observed_factories[0], previous_task_factory)
                    self.assertIs(loop.get_task_factory(), previous_task_factory)
                finally:
                    # 旧边界 RED 时也不把污染泄漏给后续测试。
                    loop.set_task_factory(previous_task_factory)

    async def _check_recorded_stdio_echo(self) -> None:
        sources = load_mcp_sdk().log_sources
        original_filters = {
            source.logger_name: tuple(logging.getLogger(source.logger_name).filters)
            for source in sources
        }
        registry = self._registry()
        audit_path = self.runtime_root / "audit" / "mcp.jsonl"
        audit = AuditLogger(audit_path)
        managers: list[MCPManager] = []
        clients: list[MCPClient] = []
        transports: list[VerifiedStdioTransport] = []
        target_client: MCPClient | None = None
        scope_token = CancellationToken()
        client_tasks: dict[str, list[asyncio.Future[object]]] = {
            "lifecycle": [],
            "operation": [],
            "cancellation": [],
        }

        def transport_factory(parameters, *, errlog, bindings):
            transport = VerifiedStdioTransport(parameters, errlog=errlog, bindings=bindings)
            transports.append(transport)
            return transport

        def client_factory(server_id, request):  # type: ignore[no-untyped-def]
            nonlocal target_client
            client = MCPClient(server_id, request, transport_factory=transport_factory)
            clients.append(client)
            target_client = client
            return client

        def manager_factory(config, context, source_env, logger):  # type: ignore[no-untyped-def]
            manager = MCPManager(
                config,
                context,
                source_env,
                logger,
                client_factory=client_factory,
            )
            managers.append(manager)
            return manager

        client_module_path = (
            self.project_root / "src" / "tricoder" / "mcp" / "client.py"
        ).resolve()

        def record_client_task(loop, coro, context=None):  # type: ignore[no-untyped-def]
            if previous_task_factory is None:
                if context is None:
                    task = asyncio.Task(coro, loop=loop)
                else:
                    task = asyncio.Task(coro, loop=loop, context=context)
            elif context is None:
                task = previous_task_factory(loop, coro)
            else:
                task = previous_task_factory(loop, coro, context=context)
            code = getattr(coro, "cr_code", None)
            source_path = Path(code.co_filename).resolve() if code is not None else None
            qualname = code.co_qualname if code is not None else ""
            if source_path == client_module_path:
                frame = getattr(coro, "cr_frame", None)
                if (
                    qualname == "MCPClient._run_lifecycle"
                    and frame is not None
                    and frame.f_locals.get("self") is target_client
                ):
                    client_tasks["lifecycle"].append(task)
                elif (
                    qualname == "_wait_for_cancellation"
                    and frame is not None
                    and frame.f_locals.get("cancellation") is scope_token
                ):
                    client_tasks["cancellation"].append(task)
                elif qualname == "_run_sdk_operation" and frame is not None:
                    # 日志边界包装独立请求 task；仍按内层 exact session 身份归属，
                    # 不能把其他 client 的请求或普通后台 task 算作本任务已回收。
                    operation = frame.f_locals.get("operation")
                    operation_code = getattr(operation, "cr_code", None)
                    operation_frame = getattr(operation, "cr_frame", None)
                    if (
                        operation_code is not None
                        and operation_code.co_qualname == "ClientSession.call_tool"
                        and operation_frame is not None
                        and target_client is not None
                        and operation_frame.f_locals.get("self") is target_client._session
                    ):
                        client_tasks["operation"].append(task)
            return task

        async def operation(active_registry: ToolRegistry) -> None:
            registry_names = {definition.name for definition in active_registry.definitions}
            self.assertIn("mcp__local_test__echo", registry_names)
            result = await active_registry.execute_async(
                "mcp__local_test__echo",
                {"text": "hello"},
                cancellation=scope_token,
                call_id="echo-call",
            )
            self.assertTrue(result.ok)
            self.assertEqual("echo:hello", result.output)
            self.assertEqual("dangerous", active_registry.origin("mcp__local_test__echo").risk)

        loop = asyncio.get_running_loop()
        original_task_factory = loop.get_task_factory()
        prior_factory_tasks: list[asyncio.Future[object]] = []

        class MarkedTask(asyncio.Task):
            """合法 factory 可使用不同的 Task 具体类型。"""

        factory_marker = object()

        async def factory_probe():  # type: ignore[no-untyped-def]
            return factory_marker

        def prior_task_factory(loop, coro, context=None):  # type: ignore[no-untyped-def]
            if original_task_factory is None and context is None:
                task = MarkedTask(coro, loop=loop)
            elif original_task_factory is None:
                task = MarkedTask(coro, loop=loop, context=context)
            elif context is None:
                task = original_task_factory(loop, coro)
            else:
                task = original_task_factory(loop, coro, context=context)
            prior_factory_tasks.append(task)
            return task

        interference_tasks: list[asyncio.Future[object]] = []
        try:
            loop.set_task_factory(prior_task_factory)
            previous_task_factory = loop.get_task_factory()
            loop.set_task_factory(record_client_task)
            interference_client = MCPClient("interference", self._launch_request())
            interference_token = CancellationToken()
            factory_probe_task = asyncio.create_task(factory_probe())
            self.assertIs(factory_probe_task, prior_factory_tasks[-1])
            self.assertIs(await factory_probe_task, factory_marker)
            interference_tasks = [
                asyncio.create_task(
                    interference_client._run_lifecycle(
                        loop.create_future(),
                        asyncio.Event(),
                    )
                ),
                asyncio.create_task(_wait_for_cancellation(interference_token)),
            ]
            for task in interference_tasks:
                task.cancel()
            await asyncio.gather(*interference_tasks, return_exceptions=True)
            await asyncio.wait_for(
                run_mcp_task(
                    self._config(),
                    registry,
                    source_env=self._source_env(),
                    audit=audit,
                    cancellation=scope_token,
                    operation=operation,
                    manager_factory=manager_factory,
                ),
                timeout=5.0,
            )
        finally:
            loop.set_task_factory(original_task_factory)
            for task in interference_tasks:
                if not task.done():
                    task.cancel()
            if interference_tasks:
                await asyncio.gather(*interference_tasks, return_exceptions=True)

        actions = [action for action, _detail in self.approvals]
        self.assertIn("dangerous_mcp_server_start", actions)
        self.assertIn("dangerous_extension_tool", actions)
        for _action, detail in self.approvals:
            self.assertNotIn(self._source_env()["PATH"], detail)
            self.assertNotIn(self._source_env()["SYSTEMROOT"], detail)
        self.assertEqual(1, len(managers))
        self.assertEqual(1, len(clients))
        self.assertTrue(prior_factory_tasks)
        for task in interference_tasks:
            self.assertTrue(task.done())
            for captured_tasks in client_tasks.values():
                self.assertNotIn(task, captured_tasks)
        dangling_mcp_tools = [
            definition.name
            for definition in registry.definitions
            if definition.name.startswith("mcp__")
            or registry.origin(definition.name).kind == "mcp"
        ]
        self.assertEqual([], dangling_mcp_tools)
        for builtin_name in ("read_file", "run_command", "finish"):
            self.assertTrue(registry.contains(builtin_name))
            self.assertEqual("builtin", registry.origin(builtin_name).kind)
        self.assertFalse(managers[0]._active)
        self.assertEqual({}, managers[0]._routes)
        self.assertEqual(MCPServerState.STOPPED, clients[0].state)
        self.assertEqual(1, len(transports))
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, transports[0].outcome.process_exit)
        self.assertTrue(transports[0].outcome.resources_closed)
        self.assertIsNone(clients[0]._lifecycle_task)
        self.assertEqual((), clients[0]._log_sources)
        self.assertFalse(clients[0]._log_operation_tasks)
        for name, filters in original_filters.items():
            self.assertEqual(filters, tuple(logging.getLogger(name).filters))
        still_running = set(asyncio.all_tasks())
        for task_kind, tasks in client_tasks.items():
            self.assertTrue(tasks, f"missing captured {task_kind} task")
            for task in tasks:
                self.assertTrue(any(task is prior for prior in prior_factory_tasks))
                self.assertTrue(task.done())
                self.assertNotIn(task, still_running)

        records = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        lifecycle = {
            (record.get("phase"), record.get("status"))
            for record in records
            if record.get("event") == "mcp_lifecycle"
        }
        self.assertTrue(
            {
                ("start", "begin"),
                ("start", "ready"),
                ("list_tools", "ok"),
                ("stop", "begin"),
                ("stop", "stopped"),
            }.issubset(lifecycle)
        )
        calls = [record for record in records if record.get("event") == "mcp_tool"]
        self.assertEqual({"begin", "ok"}, {record["status"] for record in calls})
        for record in calls:
            self.assertEqual(
                {"kind": "mcp", "id": "local_test", "risk": "dangerous"},
                record["origin"],
            )
            self.assertEqual(["text"], record["argument_keys"])
            self.assertEqual(1, record["argument_count"])
        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("hello", audit_text)
        self.assertNotIn("echo:hello", audit_text)
        self.assertNotIn("mcp-static-env-sentinel", audit_text)

    async def test_large_local_result_spills_before_returning_to_the_agent(self) -> None:
        """移除 registry 输出预算或 spill 管线时，200,000 字符会再次内联。"""

        registry = self._registry(max_output_chars=80)
        audit_path = self.runtime_root / "audit" / "large.jsonl"
        audit = AuditLogger(audit_path)

        async def operation(active_registry: ToolRegistry) -> None:
            result = await active_registry.execute_async(
                "mcp__local_test__bounded_large_output",
                {"size": 999_999},
                cancellation=CancellationToken(),
                call_id="large-call",
            )
            self.assertTrue(result.ok)
            self.assertIsNotNone(result.spill_reference)
            self.assertEqual(200_000, result.spill_bytes)
            self.assertEqual(80, result.output.count("X"))
            self.assertLess(len(result.output), 1_000)
            first = await active_registry.execute_async(
                "read_tool_result",
                {"reference": result.spill_reference},
                call_id="large-read-first",
            )
            middle = await active_registry.execute_async(
                "read_tool_result",
                {"reference": result.spill_reference, "offset": 100_000},
                call_id="large-read-middle",
            )
            tail = await active_registry.execute_async(
                "read_tool_result",
                {"reference": result.spill_reference, "offset": 199_920},
                call_id="large-read-tail",
            )
            self.assertEqual("X" * 80, first.output)
            self.assertEqual("X" * 80, middle.output)
            self.assertEqual("X" * 80, tail.output)

        await asyncio.wait_for(
            run_mcp_task(
                self._config(),
                registry,
                source_env=self._source_env(),
                audit=audit,
                cancellation=CancellationToken(),
                operation=operation,
            ),
            timeout=5.0,
        )
        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("X" * 100, audit_text)
        self.assertNotIn("mcp-static-env-sentinel", audit_text)

    async def test_delayed_local_response_uses_fixed_timeout_and_stops(self) -> None:
        """移除 call 超时或 close 时，延迟 fixture 会越过 task 的有界等待。"""

        client = MCPClient(
            "local_test",
            self._launch_request("--delay"),
            initialize_timeout=2.0,
            operation_timeout=0.1,
            cleanup_timeout=2.0,
        )
        owner_task = None
        try:
            await asyncio.wait_for(client.start(CancellationToken()), timeout=3.0)
            owner_task = client._lifecycle_task
            self.assertIsNotNone(owner_task)
            with self.assertRaisesRegex(MCPTimeoutError, r"^mcp_tool_timeout$"):
                await asyncio.wait_for(
                    client.call_tool("echo", {"text": "late"}, CancellationToken()),
                    timeout=2.0,
                )
        finally:
            await asyncio.wait_for(client.stop(), timeout=3.0)

        assert owner_task is not None
        self.assertEqual(MCPServerState.STOPPED, client.state)
        self.assertIsNone(client._lifecycle_task)
        self.assertTrue(owner_task.done())
        self.assertNotIn(owner_task, asyncio.all_tasks())

    async def test_clean_local_exit_is_a_fixed_protocol_failure(self) -> None:
        """若退出子进程被误认作已初始化 server，启动会伪成功。"""

        client = MCPClient(
            "local_test",
            self._launch_request("--exit-immediately"),
            initialize_timeout=2.0,
            operation_timeout=2.0,
            cleanup_timeout=2.0,
        )

        try:
            with self.assertRaisesRegex(MCPProtocolError, r"^mcp_protocol_error$"):
                await asyncio.wait_for(client.start(CancellationToken()), timeout=3.0)
        finally:
            await asyncio.wait_for(client.stop(), timeout=3.0)

        self.assertEqual(MCPServerState.FAILED, client.state)
        self.assertIsNone(client._lifecycle_task)


if __name__ == "__main__":
    unittest.main()
