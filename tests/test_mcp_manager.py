"""MCP 多 server manager 的生命周期、路由、审批与审计测试。"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.extensions import ExtensionKind, ExtensionTrust
from tricoder.mcp.manager import MCPManager, MCPManagerError
from tricoder.mcp.client import MCPClient, MCPCleanupError
from tricoder.mcp.models import MCPCallResult, MCPServerState, MCPToolSpec
from tricoder.mcp.tool_adapter import MCPToolHandler, normalize_tool_name
from tricoder.models import (
    AppConfig,
    ExtensionsConfig,
    MCPConfig,
    MCPServerConfig,
    ProviderConfig,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.handlers import ToolHandler
from tricoder.extensions import ToolOrigin
from tricoder.models import ToolResult


SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def _server(server_id: str, *, args: tuple[str, ...] = ()) -> MCPServerConfig:
    return MCPServerConfig(
        id=server_id,
        transport="stdio",
        command="python",
        args=args,
        enabled=True,
        trust="project",
    )


def _spec(server_id: str, raw_name: str, *, public_name: str | None = None) -> MCPToolSpec:
    return MCPToolSpec(
        server_id,
        raw_name,
        public_name or normalize_tool_name(server_id, raw_name),
        f"{server_id} tool",
        copy.deepcopy(SCHEMA),
    )


class _FakeAudit:
    def __init__(
        self,
        *,
        fail_from: int | None = None,
        fail_calls: set[int] | None = None,
    ) -> None:
        self.events: list[dict[str, object]] = []
        self.calls = 0
        self.fail_from = fail_from
        self.fail_calls = fail_calls or set()

    def log(self, event: dict[str, object]) -> None:
        self.calls += 1
        if (
            (self.fail_from is not None and self.calls >= self.fail_from)
            or self.calls in self.fail_calls
        ):
            raise OSError("AUDIT-PATH-PRIVATE-SENTINEL")
        self.events.append(copy.deepcopy(event))


class _FakeClient:
    def __init__(
        self,
        server_id: str,
        events: list[str],
        specs: tuple[MCPToolSpec, ...],
        *,
        fail_start: bool = False,
        fail_list: bool = False,
        fail_call: bool = False,
        cancel_start: bool = False,
        cancel_task_start: bool = False,
        cancel_task_stop: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self.server_id = server_id
        self.events = events
        self.specs = specs
        self.fail_start = fail_start
        self.fail_list = fail_list
        self.fail_call = fail_call
        self.cancel_start = cancel_start
        self.cancel_task_start = cancel_task_start
        self.cancel_task_stop = cancel_task_stop
        self.fail_stop = fail_stop
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.state = MCPServerState.STOPPED

    async def start(self, cancellation: CancellationToken) -> None:
        self.events.append(f"start:{self.server_id}")
        if self.cancel_start:
            cancellation.cancel()
            cancellation.raise_if_cancelled()
        if self.cancel_task_start:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        if self.fail_start:
            raise RuntimeError("CLIENT-START-PRIVATE-SENTINEL")

    async def list_tools(
        self,
        cancellation: CancellationToken,
    ) -> tuple[MCPToolSpec, ...]:
        cancellation.raise_if_cancelled()
        self.events.append(f"list:{self.server_id}")
        if self.fail_list:
            raise RuntimeError("CLIENT-LIST-PRIVATE-SENTINEL")
        return self.specs

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult:
        cancellation.raise_if_cancelled()
        self.events.append(f"call:{self.server_id}:{name}")
        self.calls.append((name, arguments))
        if self.fail_call:
            raise RuntimeError("CLIENT-CALL-PRIVATE-SENTINEL")
        nested = arguments.get("nested")
        if isinstance(nested, dict):
            nested["value"] = "server-mutated"
        return MCPCallResult(True, f"{self.server_id}:{name}")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.server_id}")
        # 既有取消夹具模拟的是已成功清理后的取消；UNKNOWN 用真实 client 单独覆盖。
        self.state = MCPServerState.STOPPED
        if self.cancel_task_stop:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        if self.fail_stop:
            raise RuntimeError("CLIENT-STOP-PRIVATE-SENTINEL")


class _ClientFactory:
    def __init__(
        self,
        events: list[str],
        specs: dict[str, tuple[MCPToolSpec, ...]],
        *,
        fail_start: set[str] | None = None,
        fail_list: set[str] | None = None,
        fail_call: set[str] | None = None,
        cancel_start: set[str] | None = None,
        cancel_task_start: set[str] | None = None,
        cancel_task_stop: set[str] | None = None,
        fail_stop: set[str] | None = None,
    ) -> None:
        self.events = events
        self.specs = specs
        self.fail_start = fail_start or set()
        self.fail_list = fail_list or set()
        self.fail_call = fail_call or set()
        self.cancel_start = cancel_start or set()
        self.cancel_task_start = cancel_task_start or set()
        self.cancel_task_stop = cancel_task_stop or set()
        self.fail_stop = fail_stop or set()
        self.clients: dict[str, _FakeClient] = {}
        self.requests: dict[str, object] = {}

    def __call__(self, server_id: str, launch_request: object) -> _FakeClient:
        self.requests[server_id] = launch_request
        client = _FakeClient(
            server_id,
            self.events,
            self.specs.get(server_id, ()),
            fail_start=server_id in self.fail_start,
            fail_list=server_id in self.fail_list,
            fail_call=server_id in self.fail_call,
            cancel_start=server_id in self.cancel_start,
            cancel_task_start=server_id in self.cancel_task_start,
            cancel_task_stop=server_id in self.cancel_task_stop,
            fail_stop=server_id in self.fail_stop,
        )
        self.clients[server_id] = client
        return client


class _PreexistingHandler(ToolHandler):
    """占用相同 public name/origin、但绝不能取得 MCP raw route 的测试 handler。"""

    name = "mcp__docs__echo"
    risk = "dangerous"
    description = "预置处理器"
    parameters = copy.deepcopy(SCHEMA)

    def run(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(True, "preexisting")


class MCPManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        self.approvals: list[tuple[str, str]] = []
        self.approve = True
        self.context = ToolContext(
            WorkspacePolicy(self.workspace),
            CommandPolicy(self.workspace),
            approver=self._approve,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approve(self, action: str, detail: str) -> bool:
        self.approvals.append((action, detail))
        return self.approve

    def _config(self, *servers: MCPServerConfig) -> AppConfig:
        return AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig("openai", "fake", "https://api.openai.com/v1", "fake"),
            extensions=ExtensionsConfig(enabled=True),
            mcp=MCPConfig(enabled=True, servers=tuple(servers)),
        )

    def _manager(
        self,
        config: AppConfig,
        factory: _ClientFactory,
        *,
        audit: _FakeAudit | None = None,
        source_env: dict[str, str] | None = None,
    ) -> MCPManager:
        return MCPManager(
            config,
            self.context,
            source_env or {},
            audit,  # type: ignore[arg-type]
            client_factory=factory,
        )

    async def test_unknown_cleanup_remains_retryable_after_cancelled_manager_stop(self):
        """manager 清理被取消时仍须关闭其余 client，并保留未知进程证据。"""
        from tests.test_mcp_client import _ControlledSDKHarness
        from tests.test_mcp_transport import ControlledClock, ControlledProcess

        clock = ControlledClock()
        self.enterContext(patch("tricoder.mcp.transport.monotonic", clock.monotonic))
        self.enterContext(patch("tricoder.mcp.transport.sleep", clock.sleep))
        harnesses = {
            "a": _ControlledSDKHarness(ControlledProcess(exit_on="terminate")),
            "b": _ControlledSDKHarness(),
        }
        harnesses["b"].session_exit_delay = 0.03
        clients = {}

        def factory(server_id, request):
            harness = harnesses[server_id]
            client = MCPClient(server_id, request, sdk_loader=harness.sdk_loader, transport_factory=harness.transport_factory)
            clients[server_id] = client
            return client

        audit = _FakeAudit()
        manager = self._manager(self._config(_server("a"), _server("b")), factory, audit=audit)
        await manager.start_all(CancellationToken())
        self.assertEqual(MCPServerState.READY, clients["b"].state)
        stopping = asyncio.create_task(manager.stop_all())
        while "session.exit" not in harnesses["b"].events:
            await asyncio.sleep(0)
        stopping.cancel("manager-stop-cancel")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await stopping
        self.assertEqual(("manager-stop-cancel",), caught.exception.args)
        self.assertEqual(MCPServerState.STOPPED, clients["a"].state)
        self.assertEqual(MCPServerState.FAILED, clients["b"].state)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await manager.stop_all()
        self.assertTrue(any(event.get("phase") == "stop" and event.get("status") == "failed" for event in audit.events))

    async def test_ordered_isolation_registration_reverse_stop_and_descriptors(self) -> None:
        """单 server 启动失败不能阻断后续 server，成功资源必须逆序且只关闭一次。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {
                "a": (_spec("a", "echo"),),
                "b": (_spec("b", "broken"),),
                "c": (_spec("c", "search"),),
            },
            fail_start={"b"},
        )
        manager = self._manager(self._config(_server("a"), _server("b"), _server("c")), factory)
        registry = ToolRegistry(self.context)

        descriptors = manager.descriptors
        await manager.start_all(CancellationToken())
        registered = manager.register_tools(registry)
        await manager.stop_all()
        await manager.stop_all()

        self.assertEqual(2, registered)
        self.assertTrue(registry.contains("mcp__a__echo"))
        self.assertTrue(registry.contains("mcp__c__search"))
        self.assertFalse(registry.contains("mcp__b__broken"))
        self.assertEqual(
            ["start:a", "list:a", "start:b", "stop:b", "start:c", "list:c", "stop:c", "stop:a"],
            events,
        )
        self.assertEqual(1, len(manager.failures))
        self.assertEqual("b", manager.failures[0].extension_id)
        self.assertEqual("start", manager.failures[0].phase)
        self.assertNotIn("CLIENT-START-PRIVATE-SENTINEL", repr(manager.failures))
        self.assertEqual(
            [("a", ExtensionKind.MCP, "mcp/a", True, ExtensionTrust.PROJECT),
             ("b", ExtensionKind.MCP, "mcp/b", True, ExtensionTrust.PROJECT),
             ("c", ExtensionKind.MCP, "mcp/c", True, ExtensionTrust.PROJECT)],
            [(item.id, item.kind, item.source, item.enabled, item.trust) for item in descriptors],
        )
        with self.assertRaisesRegex(MCPManagerError, "mcp_server_unavailable"):
            await manager.call_tool("b", "broken", {}, CancellationToken())

    async def test_list_failure_is_a_distinct_safe_phase_and_later_server_continues(self) -> None:
        """工具清单失败必须按 list_tools 审计，但对外 Host failure 仍保持固定脱敏。"""
        audit = _FakeAudit()
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"a": (_spec("a", "broken"),), "c": (_spec("c", "echo"),)},
            fail_list={"a"},
        )
        manager = self._manager(
            self._config(_server("a"), _server("c")),
            factory,
            audit=audit,
        )
        registry = ToolRegistry(self.context)

        await manager.start_all(CancellationToken())

        self.assertEqual(1, manager.register_tools(registry))
        self.assertTrue(registry.contains("mcp__c__echo"))
        self.assertEqual(
            ["start:a", "list:a", "stop:a", "start:c", "list:c"],
            events,
        )
        failed_audits = [
            event
            for event in audit.events
            if event.get("server_id") == "a" and event.get("status") == "failed"
        ]
        self.assertEqual("list_tools", failed_audits[-1]["phase"])
        self.assertNotIn("CLIENT-LIST-PRIVATE-SENTINEL", json.dumps(audit.events))

    async def test_exact_route_copies_arguments_and_rejects_unknown_targets(self) -> None:
        """只有已注册的 exact server/raw-name 对可调用，且 server 不能修改调用方参数。"""
        events: list[str] = []
        factory = _ClientFactory(events, {"docs": (_spec("docs", "Read Page"),)})
        manager = self._manager(self._config(_server("docs")), factory)
        registry = ToolRegistry(self.context)
        token = CancellationToken()
        await manager.start_all(token)
        manager.register_tools(registry)
        arguments: dict[str, object] = {"query": "x", "nested": {"value": "original"}}

        result = await manager.call_tool("docs", "Read Page", arguments, token)

        self.assertTrue(result.ok)
        self.assertEqual("original", arguments["nested"]["value"])  # type: ignore[index]
        self.assertEqual("server-mutated", factory.clients["docs"].calls[0][1]["nested"]["value"])  # type: ignore[index]
        for server_id, raw_name, expected in (
            ("unknown", "Read Page", "mcp_server_unavailable"),
            ("docs", "read page", "mcp_tool_unknown"),
        ):
            with self.subTest(server_id=server_id, raw_name=raw_name):
                with self.assertRaisesRegex(MCPManagerError, expected):
                    await manager.call_tool(server_id, raw_name, {}, token)
        await manager.stop_all()
        with self.assertRaisesRegex(MCPManagerError, "mcp_server_unavailable"):
            await manager.call_tool("docs", "Read Page", {}, token)

    async def test_unexpected_client_call_error_is_fixed_and_redacted(self) -> None:
        """注入 client 违反固定错误契约时，manager 仍不能把底层正文交给上层。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"docs": (_spec("docs", "echo"),)},
            fail_call={"docs"},
        )
        manager = self._manager(self._config(_server("docs")), factory)
        registry = ToolRegistry(self.context)
        token = CancellationToken()
        await manager.start_all(token)
        manager.register_tools(registry)

        with self.assertRaises(MCPManagerError) as captured:
            await manager.call_tool(
                "docs",
                "echo",
                {"query": "ARGUMENT-PRIVATE-SENTINEL"},
                token,
            )

        self.assertEqual("mcp_tool_failed", str(captured.exception))
        self.assertNotIn("CLIENT-CALL-PRIVATE-SENTINEL", str(captured.exception))
        self.assertNotIn("ARGUMENT-PRIVATE-SENTINEL", str(captured.exception))

    async def test_same_server_collision_rejects_both_but_namespaces_other_servers(self) -> None:
        """规范名冲突必须双拒绝；不同 server 的同名工具必须保留各自命名空间。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {
                "a": (_spec("a", "Echo Tool"), _spec("a", "echo-tool")),
                "c": (_spec("c", "Echo Tool"),),
            },
        )
        manager = self._manager(self._config(_server("a"), _server("c")), factory)
        registry = ToolRegistry(self.context)
        await manager.start_all(CancellationToken())

        registered = manager.register_tools(registry)

        self.assertEqual(1, registered)
        self.assertFalse(registry.contains("mcp__a__echo_tool"))
        self.assertTrue(registry.contains("mcp__c__echo_tool"))
        conflicts = [failure for failure in manager.failures if failure.phase == "tool_conflict"]
        self.assertEqual({"a"}, {failure.extension_id for failure in conflicts})
        with self.assertRaisesRegex(MCPManagerError, "mcp_tool_unknown"):
            await manager.call_tool("a", "Echo Tool", {}, CancellationToken())

    async def test_builtin_collision_is_recorded_without_replacing_builtin(self) -> None:
        """即使 client 交回异常 public name，内置工具也必须优先且冲突不得进入路由。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"override": (_spec("override", "read", public_name="read_file"),)},
        )
        manager = self._manager(self._config(_server("override")), factory)
        registry = ToolRegistry(self.context)
        await manager.start_all(CancellationToken())

        self.assertEqual(0, manager.register_tools(registry))
        self.assertEqual("builtin", registry.origin("read_file").kind)
        self.assertTrue(any(failure.phase == "tool_conflict" for failure in manager.failures))
        with self.assertRaisesRegex(MCPManagerError, "mcp_tool_unknown"):
            await manager.call_tool("override", "read", {}, CancellationToken())

    async def test_preexisting_same_name_and_origin_never_creates_raw_route(self) -> None:
        """同名同 origin 预置 handler 不能被误判为本次 Host 成功注册的 MCP handler。"""
        events: list[str] = []
        factory = _ClientFactory(events, {"docs": (_spec("docs", "echo"),)})
        manager = self._manager(self._config(_server("docs")), factory)
        registry = ToolRegistry(self.context)
        registry.register(
            _PreexistingHandler(self.context),
            origin=ToolOrigin("mcp", "docs", "dangerous"),
        )
        await manager.start_all(CancellationToken())

        registered = manager.register_tools(registry)
        removed = manager.unregister_tools(registry)

        self.assertEqual(0, registered)
        self.assertEqual(0, removed)
        self.assertTrue(registry.contains("mcp__docs__echo"))
        self.assertTrue(any(failure.phase == "tool_conflict" for failure in manager.failures))
        with self.assertRaisesRegex(MCPManagerError, "mcp_tool_unknown"):
            await manager.call_tool("docs", "echo", {"query": "x"}, CancellationToken())
        self.assertEqual([], factory.clients["docs"].calls)

    async def test_unregister_removes_only_handlers_registered_by_this_manager(self) -> None:
        """task scope 撤销使用真实注册身份，内置工具与非本轮对象必须保留。"""
        events: list[str] = []
        factory = _ClientFactory(events, {"docs": (_spec("docs", "echo"),)})
        manager = self._manager(self._config(_server("docs")), factory)
        registry = ToolRegistry(self.context)
        await manager.start_all(CancellationToken())
        self.assertEqual(1, manager.register_tools(registry))

        self.assertEqual(1, manager.unregister_tools(registry))

        self.assertFalse(registry.contains("mcp__docs__echo"))
        self.assertTrue(registry.contains("read_file"))
        self.assertEqual(0, manager.unregister_tools(registry))
        with self.assertRaisesRegex(MCPManagerError, "mcp_tool_unknown"):
            await manager.call_tool("docs", "echo", {"query": "x"}, CancellationToken())

    async def test_untrusted_host_result_cannot_claim_preexisting_or_duplicate_handler(self) -> None:
        """Host 返回值不能伪造本轮注册凭据、删除预存对象或夸大注册计数。"""
        events: list[str] = []
        factory = _ClientFactory(events, {"docs": (_spec("docs", "echo"),)})
        manager = self._manager(self._config(_server("docs")), factory)
        registry = ToolRegistry(self.context)
        foreign_manager = object()
        preexisting = MCPToolHandler(
            self.context,
            foreign_manager,  # type: ignore[arg-type]
            _spec("foreign", "keep"),
        )
        registry.register(
            preexisting,
            origin=ToolOrigin("mcp", "foreign", "dangerous"),
        )
        await manager.start_all(CancellationToken())
        expected = manager._extensions[0].tool_handlers()[0]

        class IncorrectHost:
            def register_tool_handlers(
                self,
                _registry: ToolRegistry,
                *,
                registered_sink: list[ToolHandler] | None = None,
            ) -> tuple[ToolHandler, ...]:
                _registry.register(
                    expected,
                    origin=ToolOrigin("mcp", "docs", "dangerous"),
                )
                if registered_sink is not None:
                    registered_sink.extend((preexisting, preexisting))
                return (preexisting, preexisting)

        manager._host = IncorrectHost()  # type: ignore[assignment]

        self.assertEqual(1, manager.register_tools(registry))
        self.assertEqual(1, manager.unregister_tools(registry))
        self.assertFalse(registry.contains(expected.name))
        self.assertTrue(registry.contains(preexisting.name))
        self.assertIs(preexisting, registry._handlers[preexisting.name])

    async def test_cancel_propagates_and_rolls_back_current_and_ready_servers(self) -> None:
        """启动期间取消必须清理当前部分 server 与此前 ready server，并原样传播。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"a": (_spec("a", "echo"),), "b": (_spec("b", "echo"),)},
            cancel_start={"b"},
        )
        manager = self._manager(self._config(_server("a"), _server("b")), factory)
        token = CancellationToken()

        with self.assertRaises(CancellationError):
            await manager.start_all(token)

        self.assertEqual(["start:a", "list:a", "start:b", "stop:b", "stop:a"], events)
        self.assertEqual(0, manager.register_tools(ToolRegistry(self.context)))

    async def test_asyncio_task_cancellation_also_rolls_back_all_started_clients(self) -> None:
        """调用方直接取消 start task 时也必须回收当前 client 和此前 ready client。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"a": (_spec("a", "echo"),), "b": (_spec("b", "echo"),)},
            cancel_task_start={"b"},
        )
        manager = self._manager(self._config(_server("a"), _server("b")), factory)

        with self.assertRaises(asyncio.CancelledError):
            await manager.start_all(CancellationToken())

        self.assertEqual(["start:a", "list:a", "start:b", "stop:b", "stop:a"], events)
        self.assertEqual(0, manager.register_tools(ToolRegistry(self.context)))

    async def test_cancelled_current_provider_failed_cleanup_remains_retryable_by_manager(self) -> None:
        """当前 provider 取消且 cleanup 失败后，manager 后续 stop 必须仍能重试该 client。"""
        for mode in ("token", "task"):
            with self.subTest(mode=mode):
                events: list[str] = []
                factory = _ClientFactory(
                    events,
                    {"a": (_spec("a", "echo"),), "b": (_spec("b", "echo"),)},
                    cancel_start={"b"} if mode == "token" else set(),
                    cancel_task_start={"b"} if mode == "task" else set(),
                    fail_stop={"b"},
                )
                manager = self._manager(
                    self._config(_server("a"), _server("b")),
                    factory,
                )
                expected = CancellationError if mode == "token" else asyncio.CancelledError

                with self.assertRaises(expected):
                    await manager.start_all(CancellationToken())

                factory.clients["b"].fail_stop = False
                await manager.stop_all()

                self.assertEqual(1, events.count("stop:a"))
                self.assertEqual(2, events.count("stop:b"))

    async def test_stop_failure_continues_reverse_cleanup_and_only_failed_server_is_retried(self) -> None:
        """一个 stop 失败不能阻断其余清理；重试不得再次停止已成功 server。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"a": (_spec("a", "echo"),), "c": (_spec("c", "echo"),)},
            fail_stop={"a"},
        )
        manager = self._manager(self._config(_server("a"), _server("c")), factory)
        await manager.start_all(CancellationToken())

        with self.assertRaisesRegex(MCPCleanupError, "mcp_cleanup_failed"):
            await manager.stop_all()
        factory.clients["a"].fail_stop = False
        await manager.stop_all()
        await manager.stop_all()

        self.assertEqual(
            ["start:a", "list:a", "start:c", "list:c", "stop:c", "stop:a", "stop:a"],
            events,
        )
        stop_failures = [failure for failure in manager.failures if failure.phase == "stop"]
        self.assertEqual(1, len(stop_failures))
        self.assertNotIn("CLIENT-STOP-PRIVATE-SENTINEL", repr(manager.failures))

    async def test_asyncio_cancellation_during_stop_cleans_remaining_servers_then_propagates(self) -> None:
        """client stop 重新抛出调用方取消时，Host 仍须继续反向关闭其他 server。"""
        events: list[str] = []
        factory = _ClientFactory(
            events,
            {"a": (_spec("a", "echo"),), "c": (_spec("c", "echo"),)},
            cancel_task_stop={"c"},
        )
        manager = self._manager(self._config(_server("a"), _server("c")), factory)
        await manager.start_all(CancellationToken())

        with self.assertRaises(asyncio.CancelledError):
            await manager.stop_all()

        self.assertEqual(
            ["start:a", "list:a", "start:c", "list:c", "stop:c", "stop:a"],
            events,
        )

    async def test_repeated_start_and_register_do_not_duplicate_clients_handlers_or_failures(self) -> None:
        """一个 manager 作用域内重复调用生命周期 API 不能产生缓存或失败漂移。"""
        events: list[str] = []
        factory = _ClientFactory(events, {"a": (_spec("a", "echo"),)})
        manager = self._manager(self._config(_server("a")), factory)
        registry = ToolRegistry(self.context)
        token = CancellationToken()

        await manager.start_all(token)
        await manager.start_all(token)
        first = manager.register_tools(registry)
        second = manager.register_tools(registry)

        self.assertEqual((1, 0), (first, second))
        self.assertEqual(["start:a", "list:a"], events)
        self.assertEqual((), manager.failures)

    async def test_startup_audit_is_fail_closed_and_later_audit_failure_cannot_block_cleanup(self) -> None:
        """首条生命周期审计失败时不得创建 client，后续审计故障不得阻断反向清理。"""
        blocked_factory = _ClientFactory([], {"a": (_spec("a", "echo"),)})
        blocked = self._manager(
            self._config(_server("a")),
            blocked_factory,
            audit=_FakeAudit(fail_from=1),
        )

        await blocked.start_all(CancellationToken())

        self.assertEqual({}, blocked_factory.clients)
        self.assertEqual("start", blocked.failures[0].phase)

        events: list[str] = []
        later_factory = _ClientFactory(
            events,
            {"a": (_spec("a", "echo"),), "c": (_spec("c", "echo"),)},
        )
        later = self._manager(
            self._config(_server("a"), _server("c")),
            later_factory,
            # 两个 server 启动/列举工具共写 6 条；从 stop:C 的 begin
            # 开始模拟后续审计不可写，真实 client 清理仍必须继续。
            audit=_FakeAudit(fail_calls={7, 8, 9, 10}),
        )
        await later.start_all(CancellationToken())
        await later.stop_all()

        self.assertEqual(["start:a", "list:a", "start:c", "list:c", "stop:c", "stop:a"], events)

    async def test_lifecycle_and_tool_audit_are_bounded_and_never_contain_values(self) -> None:
        """审计只能保留固定类别、摘要、大小和参数键，不能保存 argv/env/参数/异常正文。"""
        audit = _FakeAudit()
        events: list[str] = []
        raw_tool = "Private Tool Name"
        factory = _ClientFactory(events, {"docs": (_spec("docs", raw_tool),)})
        config = self._config(_server("docs", args=("--label=ARGV-PRIVATE-SENTINEL",)))
        manager = self._manager(
            config,
            factory,
            audit=audit,
            source_env={"MCP_PRIVATE": "ENV-PRIVATE-SENTINEL"},
        )
        registry = ToolRegistry(self.context)
        token = CancellationToken()
        await manager.start_all(token)
        manager.register_tools(registry)
        await manager.call_tool(
            "docs",
            raw_tool,
            {"query": "ARGUMENT-PRIVATE-SENTINEL"},
            token,
        )
        await manager.stop_all()

        serialized = json.dumps(audit.events, ensure_ascii=False)
        for sentinel in (
            "ARGV-PRIVATE-SENTINEL",
            "ENV-PRIVATE-SENTINEL",
            "ARGUMENT-PRIVATE-SENTINEL",
            "CLIENT-START-PRIVATE-SENTINEL",
            raw_tool,
        ):
            self.assertNotIn(sentinel, serialized)
        lifecycle_allowed = {
            "event", "server_id", "phase", "status", "duration_ms",
            "tool_name_digest", "tool_name_chars", "tool_count",
        }
        lifecycle = [event for event in audit.events if event.get("event") == "mcp_lifecycle"]
        self.assertTrue(lifecycle)
        self.assertTrue(all(set(event) <= lifecycle_allowed for event in lifecycle))
        tool_event = next(event for event in audit.events if event.get("event") == "mcp_tool")
        self.assertEqual(
            {"kind": "mcp", "id": "docs", "risk": "dangerous"},
            tool_event["origin"],
        )
        self.assertEqual(["query"], tool_event["argument_keys"])

    async def test_tool_approval_is_never_auto_granted_by_permission_mode(self) -> None:
        """strict、relaxed、fullaccess 都必须由危险扩展审批动作显式放行。"""
        for mode in ("strict", "relaxed", "fullaccess"):
            with self.subTest(mode=mode):
                approvals: list[tuple[str, str]] = []
                context = ToolContext(
                    WorkspacePolicy(self.workspace),
                    CommandPolicy(self.workspace),
                    approver=lambda action, detail: approvals.append((action, detail)) or False,
                    auto_approve_git=lambda _args: True,
                )
                events: list[str] = []
                factory = _ClientFactory(events, {"docs": (_spec("docs", "echo"),)})
                manager = MCPManager(
                    self._config(_server("docs")),
                    context,
                    {},
                    None,
                    client_factory=factory,
                )
                # 启动审批属于另一条边界，此测试只让它通过，再拒绝工具审批。
                context.approver = lambda action, detail: (
                    True
                    if action == "dangerous_mcp_server_start"
                    else approvals.append((action, detail)) or False
                )
                registry = ToolRegistry(context)
                await manager.start_all(CancellationToken())
                manager.register_tools(registry)

                result = await registry.execute_async(
                    "mcp__docs__echo",
                    {"query": "must-not-run"},
                    cancellation=CancellationToken(),
                )

                self.assertFalse(result.ok)
                self.assertEqual("用户拒绝执行扩展工具", result.output)
                self.assertEqual("dangerous_extension_tool", approvals[-1][0])
                self.assertFalse(any(event.startswith("call:") for event in events))


if __name__ == "__main__":
    unittest.main()
