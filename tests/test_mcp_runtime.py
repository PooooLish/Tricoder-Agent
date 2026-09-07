import asyncio
import gc
import json
import tempfile
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.extensions import ToolOrigin
from tricoder.mcp.client import MCPClient, MCPCleanupError
from tricoder.mcp.manager import MCPManager
from tricoder.mcp.runtime import run_mcp_task, run_mcp_task_sync
from tricoder.mcp.sdk import MCPDependencyError
from tricoder.mcp.models import MCPServerState, MCPToolSpec
from tricoder.mcp.tool_adapter import MCPToolHandler
from tricoder.models import (
    AppConfig,
    ExtensionsConfig,
    MCPConfig,
    MCPServerConfig,
    ProviderConfig,
    ToolResult,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.handlers import ToolHandler


class _FakeManager:
    """只记录 task scope 生命周期，不启动真实进程。"""

    def __init__(
        self,
        events,
        *,
        start_error=None,
        register_error=None,
        stop_error=None,
    ):  # type: ignore[no-untyped-def]
        self.events = events
        self.start_error = start_error
        self.register_error = register_error
        self.stop_error = stop_error

    async def start_all(self, _cancellation: CancellationToken) -> None:
        self.events.append("start")
        if self.start_error is not None:
            raise self.start_error

    def register_tools(self, _registry: ToolRegistry) -> int:
        self.events.append("register")
        if self.register_error is not None:
            raise self.register_error
        return 0

    def unregister_tools(self, _registry: ToolRegistry) -> int:
        return 0

    async def stop_all(self) -> None:
        self.events.append("stop")
        if self.stop_error is not None:
            raise self.stop_error


class _ScopedHandler(ToolHandler):
    """持有 manager 的测试 handler，用于验证作用域退出后没有强引用残留。"""

    name = "mcp__docs__scoped"
    description = "scoped fake"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, context: ToolContext, manager: object) -> None:
        super().__init__(context)
        self.manager = manager

    def run(self, _arguments):  # type: ignore[no-untyped-def]
        return ToolResult(True, "scoped")


class _ScopedManager:
    """模拟 manager 精确记录并撤销本轮实际注册的 handler。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.handler: _ScopedHandler | None = _ScopedHandler(registry.context, self)
        self.registered = False

    async def start_all(self, _cancellation: CancellationToken) -> None:
        return None

    def register_tools(self, registry: ToolRegistry) -> int:
        assert self.handler is not None
        try:
            registry.register(
                self.handler,
                origin=ToolOrigin("mcp", "docs", "dangerous"),
            )
        except ValueError:
            return 0
        self.registered = True
        return 1

    def unregister_tools(self, registry: ToolRegistry) -> int:
        handler = self.handler
        removed = bool(
            self.registered
            and handler is not None
            and registry.unregister(handler)
        )
        self.registered = False
        self.handler = None
        return int(removed)

    async def stop_all(self) -> None:
        self.handler = None


class _FailingAudit:
    """任何审计写入都失败，验证固定清理记录保持 best-effort。"""

    def log(self, _event: dict[str, object]) -> None:
        raise OSError("AUDIT-PRIVATE-SENTINEL")


class _PartialRegistrationHost:
    """先真实注册一个 MCP handler，再抛出同一个注册异常。"""

    def __init__(
        self,
        manager: MCPManager,
        primary: BaseException,
        events: list[str],
        handler_refs: list[weakref.ReferenceType[ToolHandler]],
    ) -> None:
        self.manager = manager
        self.primary = primary
        self.events = events
        self.handler_refs = handler_refs
        self.failures = ()

    async def start(self, _cancellation: CancellationToken) -> None:
        self.events.append("start")

    def register_tool_handlers(
        self,
        registry: ToolRegistry,
    ) -> tuple[ToolHandler, ...]:
        spec = MCPToolSpec(
            "docs",
            "partial",
            "mcp__docs__partial",
            "partial registration",
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        handler = MCPToolHandler(registry.context, self.manager, spec)
        registry.register(
            handler,
            origin=ToolOrigin("mcp", "docs", "dangerous"),
        )
        self.handler_refs.append(weakref.ref(handler))
        self.events.append("registered-one")
        registry.unregister(handler)
        raise self.primary

    async def stop(self) -> None:
        self.events.append("stop")


class MCPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        self.config = AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig("openai", "test-key", "https://example.test", "test"),
            extensions=ExtensionsConfig(enabled=True),
            mcp=MCPConfig(
                enabled=True,
                servers=(
                    MCPServerConfig("docs", "stdio", "python", enabled=True),
                ),
            ),
        )
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                lambda _action, _detail: False,
            )
        )
        self.token = CancellationToken()

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_async_scope_starts_registers_runs_and_always_stops(self) -> None:
        events: list[str] = []

        async def operation(active_registry: ToolRegistry) -> str:
            self.assertIs(self.registry, active_registry)
            events.append("operation")
            return "done"

        result = await run_mcp_task(
            self.config,
            self.registry,
            source_env={},
            audit=None,
            cancellation=self.token,
            operation=operation,
            manager_factory=lambda *_args: _FakeManager(events),
        )

        self.assertEqual("done", result)
        self.assertEqual(["start", "register", "operation", "stop"], events)

    async def test_operation_error_is_preserved_when_cleanup_also_fails(self) -> None:
        events: list[str] = []
        primary = ValueError("PRIMARY-SENTINEL")

        async def operation(_registry: ToolRegistry) -> str:
            events.append("operation")
            raise primary

        with self.assertRaises(ValueError) as captured:
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=None,
                cancellation=self.token,
                operation=operation,
                manager_factory=lambda *_args: _FakeManager(
                    events,
                    stop_error=RuntimeError("CLEANUP-SENTINEL"),
                ),
            )

        self.assertIs(primary, captured.exception)
        self.assertEqual(["start", "register", "operation", "stop"], events)

    async def test_real_client_startup_cleanup_failure_prevents_task_success(self) -> None:
        """真实 manager 隔离启动故障后，runtime 仍须看见真实 client 的关闭失败。"""
        from tests.test_mcp_client import _FakeSDKHarness

        harness = _FakeSDKHarness()
        harness.session.initialize_error = RuntimeError("initialize-private")
        harness.session_exit_error = RuntimeError("cleanup-private")
        self.registry.context.approver = lambda _action, _detail: True

        def factory(config, context, source_env, audit):
            return MCPManager(
                config, context, source_env, audit,
                client_factory=lambda server_id, request: MCPClient(
                    server_id, request, sdk_loader=harness.sdk_loader,
                    transport_factory=harness.transport_factory,
                ),
            )

        async def operation(_registry):
            return "must-not-report-success"

        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await run_mcp_task(
                self.config, self.registry, source_env={}, audit=None,
                cancellation=self.token, operation=operation, manager_factory=factory,
            )

    async def check_unknown_runtime_cleanup(self, primary=None):
        """真实 runtime/manager/client 图中注入受控进程，审计仅检查安全元数据。"""
        from tests.test_mcp_client import _ControlledSDKHarness
        from tests.test_mcp_transport import ControlledClock

        clock = ControlledClock()
        self.enterContext(mock.patch("tricoder.mcp.transport.monotonic", clock.monotonic))
        self.enterContext(mock.patch("tricoder.mcp.transport.sleep", clock.sleep))
        harness = _ControlledSDKHarness()
        harness.session.call_result = {
            "content": [{"type": "text", "text": "PROTOCOL-PRIVATE-SENTINEL"}],
            "isError": False,
        }
        harness.process_harness.process.pid = "PROCESS-PRIVATE-SENTINEL"
        events = []
        audit = SimpleNamespace(log=events.append)
        clients = []
        managers = []
        self.registry.context.approver = lambda _action, _detail: True

        def client_factory(server_id, request):
            client = MCPClient(server_id, request, sdk_loader=harness.sdk_loader, transport_factory=harness.transport_factory)
            clients.append(client)
            return client

        def factory(config, context, source_env, audit):
            manager = MCPManager(config, context, source_env, audit, client_factory=client_factory)
            managers.append(manager)
            return manager

        async def operation(_registry):
            self.assertEqual(MCPServerState.READY, clients[0].state)
            result = await managers[0].call_tool("docs", "Echo Tool", {"text": "ARGUMENT-PRIVATE-SENTINEL"}, self.token)
            self.assertTrue(result.ok)
            if primary is not None:
                raise primary
            return "must-not-report-success"

        expected = type(primary) if primary is not None else MCPCleanupError
        with self.assertRaises(expected) as captured:
            await run_mcp_task(
                self.config, self.registry, source_env={}, audit=audit,
                cancellation=self.token, operation=operation, manager_factory=factory,
            )
        if primary is not None:
            self.assertIs(primary, captured.exception)
        else:
            self.assertEqual("mcp_cleanup_failed", str(captured.exception))
        self.assertEqual(MCPServerState.FAILED, clients[0].state)
        self.assertIn({"event": "mcp_lifecycle", "phase": "task_cleanup", "status": "failed", "error": "mcp_cleanup_failed"}, events)
        self.assertTrue(any(event.get("phase") == "stop" and event.get("status") == "failed" for event in events))
        for marker in ("PROTOCOL-PRIVATE-SENTINEL", "PROCESS-PRIVATE-SENTINEL", "ARGUMENT-PRIVATE-SENTINEL", "PRIMARY-PRIVATE-SENTINEL"):
            self.assertNotIn(marker, json.dumps(events))
        self.assertFalse(any(definition.name.startswith("mcp__") for definition in self.registry.definitions))
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await managers[0].stop_all()

    async def test_unknown_cleanup_replaces_success_through_manager_and_runtime(self):
        await self.check_unknown_runtime_cleanup()

    async def test_unknown_cleanup_does_not_replace_primary_cancelled_error(self):
        await self.check_unknown_runtime_cleanup(asyncio.CancelledError("PRIMARY-PRIVATE-SENTINEL"))
        self.assertTrue(self.token.is_cancelled)

    async def test_unknown_cleanup_does_not_replace_primary_business_error(self):
        await self.check_unknown_runtime_cleanup(ValueError("PRIMARY-PRIVATE-SENTINEL"))

    async def test_unknown_cleanup_does_not_replace_primary_token_cancellation(self):
        await self.check_unknown_runtime_cleanup(CancellationError("PRIMARY-PRIVATE-SENTINEL"))

    async def test_success_is_replaced_by_fixed_cleanup_failure(self) -> None:
        events: list[str] = []

        async def operation(_registry: ToolRegistry) -> str:
            events.append("operation")
            return "must-not-escape"

        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=None,
                cancellation=self.token,
                operation=operation,
                manager_factory=lambda *_args: _FakeManager(
                    events,
                    stop_error=RuntimeError("SECRET-CLEANUP-SENTINEL"),
                ),
            )

        self.assertEqual(["start", "register", "operation", "stop"], events)

    async def test_cleanup_cancellation_without_primary_is_propagated_unchanged(self) -> None:
        """清理被取消不是普通 cleanup failure，不能伪装成 MCPCleanupError。"""
        primary = asyncio.CancelledError()

        async def operation(_registry: ToolRegistry) -> str:
            return "done"

        with self.assertRaises(asyncio.CancelledError) as captured:
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=None,
                cancellation=self.token,
                operation=operation,
                manager_factory=lambda *_args: _FakeManager([], stop_error=primary),
            )

        self.assertIs(primary, captured.exception)

    async def test_cleanup_cancellation_does_not_replace_existing_primary(self) -> None:
        """主异常与清理取消并存时，调用方必须收到同一个主异常对象。"""
        primary = ValueError("PRIMARY-SENTINEL")

        async def operation(_registry: ToolRegistry) -> str:
            raise primary

        with self.assertRaises(ValueError) as captured:
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=None,
                cancellation=self.token,
                operation=operation,
                manager_factory=lambda *_args: _FakeManager(
                    [],
                    stop_error=asyncio.CancelledError(),
                ),
            )

        self.assertIs(primary, captured.exception)

    async def test_cancellation_still_runs_cleanup_and_preserves_primary(self) -> None:
        events: list[str] = []
        primary = asyncio.CancelledError()

        async def operation(_registry: ToolRegistry) -> str:
            events.append("operation")
            raise primary

        with self.assertRaises(asyncio.CancelledError) as captured:
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=None,
                cancellation=self.token,
                operation=operation,
                manager_factory=lambda *_args: _FakeManager(events),
            )

        self.assertIs(primary, captured.exception)
        self.assertEqual(["start", "register", "operation", "stop"], events)

    async def test_start_and_register_failures_both_run_stop(self) -> None:
        """初始化任何阶段失败都必须把已创建 manager 交给统一清理出口。"""
        for phase in ("start", "register"):
            with self.subTest(phase=phase):
                events: list[str] = []
                kwargs = {
                    f"{phase}_error": RuntimeError(f"{phase}-failure")
                }

                async def operation(_registry: ToolRegistry) -> str:
                    events.append("operation")
                    return "must-not-run"

                with self.assertRaisesRegex(RuntimeError, f"{phase}-failure"):
                    await run_mcp_task(
                        self.config,
                        self.registry,
                        source_env={},
                        audit=None,
                        cancellation=self.token,
                        operation=operation,
                        manager_factory=lambda *_args, kwargs=kwargs: _FakeManager(
                            events,
                            **kwargs,
                        ),
                    )

                expected = ["start", "stop"] if phase == "start" else ["start", "register", "stop"]
                self.assertEqual(expected, events)

    async def test_cleanup_audit_failure_never_replaces_primary_exception(self) -> None:
        """best-effort 审计不可把业务根因替换成审计存储错误。"""
        primary = LookupError("PRIMARY-SENTINEL")

        async def operation(_registry: ToolRegistry) -> str:
            raise primary

        with self.assertRaises(LookupError) as captured:
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=_FailingAudit(),  # type: ignore[arg-type]
                cancellation=self.token,
                operation=operation,
                manager_factory=lambda *_args: _FakeManager(
                    [],
                    stop_error=RuntimeError("cleanup-failure"),
                ),
            )

        self.assertIs(primary, captured.exception)

    async def test_scope_removes_only_its_handler_and_releases_manager_graph(self) -> None:
        """退出后 registry 不得继续强引用已停止 manager；本轮 handler 应消失。"""
        manager_ref = None
        handler_ref = None

        def factory(*_args):  # type: ignore[no-untyped-def]
            nonlocal manager_ref, handler_ref
            manager = _ScopedManager(self.registry)
            assert manager.handler is not None
            manager_ref = weakref.ref(manager)
            handler_ref = weakref.ref(manager.handler)
            return manager

        async def operation(registry: ToolRegistry) -> str:
            self.assertTrue(registry.contains("mcp__docs__scoped"))
            return "done"

        result = await run_mcp_task(
            self.config,
            self.registry,
            source_env={},
            audit=None,
            cancellation=self.token,
            operation=operation,
            manager_factory=factory,
        )

        self.assertEqual("done", result)
        self.assertFalse(self.registry.contains("mcp__docs__scoped"))
        gc.collect()
        assert manager_ref is not None and handler_ref is not None
        self.assertIsNone(handler_ref())
        self.assertIsNone(manager_ref())

    async def test_scope_does_not_remove_preexisting_same_name_handler(self) -> None:
        """注册冲突时 manager 没有所有权，清理不得删除预存 handler。"""
        preexisting_manager = object()
        preexisting = _ScopedHandler(self.registry.context, preexisting_manager)
        self.registry.register(
            preexisting,
            origin=ToolOrigin("mcp", "docs", "dangerous"),
        )

        async def operation(registry: ToolRegistry) -> str:
            self.assertIs(preexisting, registry._handlers[preexisting.name])
            return "done"

        await run_mcp_task(
            self.config,
            self.registry,
            source_env={},
            audit=None,
            cancellation=self.token,
            operation=operation,
            manager_factory=lambda *_args: _ScopedManager(self.registry),
        )

        self.assertTrue(self.registry.contains(preexisting.name))
        self.assertIs(preexisting, self.registry._handlers[preexisting.name])

    async def test_partial_registration_failure_is_transactionally_unregistered(self) -> None:
        """Host 第 N 项异常时，前 N-1 项也必须进入 manager 的身份撤销清单。"""
        primary = RuntimeError("PARTIAL-REGISTER-PRIMARY")
        events: list[str] = []
        manager_refs: list[weakref.ReferenceType[MCPManager]] = []
        handler_refs: list[weakref.ReferenceType[ToolHandler]] = []

        def factory(config, context, source_env, audit):  # type: ignore[no-untyped-def]
            manager = MCPManager(config, context, source_env, audit)
            manager._host = _PartialRegistrationHost(
                manager,
                primary,
                events,
                handler_refs,
            )  # type: ignore[assignment]
            manager_refs.append(weakref.ref(manager))
            return manager

        async def forbidden_operation(_registry: ToolRegistry) -> str:
            raise AssertionError("注册失败后不得进入 operation")

        with self.assertRaises(RuntimeError) as captured:
            await run_mcp_task(
                self.config,
                self.registry,
                source_env={},
                audit=None,
                cancellation=self.token,
                operation=forbidden_operation,
                manager_factory=factory,
            )

        self.assertIs(primary, captured.exception)
        self.assertEqual(["start", "registered-one", "stop"], events)
        self.assertFalse(self.registry.contains("mcp__docs__partial"))
        primary.__traceback__ = None
        gc.collect()
        self.assertIsNone(handler_refs[0]())
        self.assertIsNone(manager_refs[0]())

    async def test_sync_entry_rejects_an_already_running_loop_without_asyncio_run(self) -> None:
        async def operation(_registry: ToolRegistry) -> str:
            return "done"

        with mock.patch("tricoder.mcp.runtime.asyncio.run") as run:
            with self.assertRaisesRegex(RuntimeError, "已运行的事件循环"):
                run_mcp_task_sync(
                    self.config,
                    self.registry,
                    source_env={},
                    audit=None,
                    cancellation=self.token,
                    operation=operation,
                    manager_factory=lambda *_args: _FakeManager([]),
                )
        run.assert_not_called()

    async def test_default_runtime_preflights_missing_sdk_before_manager_or_operation(self) -> None:
        """默认真实 manager 不得把 SDK 缺失隔离成“零工具成功”。"""
        operated = False

        async def operation(_registry: ToolRegistry) -> str:
            nonlocal operated
            operated = True
            return "incorrect-success"

        with mock.patch(
            "tricoder.mcp.runtime.load_mcp_sdk",
            side_effect=MCPDependencyError("PRIVATE-IMPORT-DETAIL"),
            create=True,
        ):
            with self.assertRaises(MCPDependencyError):
                await run_mcp_task(
                    self.config,
                    self.registry,
                    source_env={},
                    audit=None,
                    cancellation=self.token,
                    operation=operation,
                )

        self.assertFalse(operated)

    async def test_real_manager_runtime_scope_is_bounded_when_local_server_exits_cleanly(self) -> None:
        """若 runtime 误把 server 退出当成功连接，此 scope 会暴露伪造的动态工具。"""
        project_root = Path(__file__).resolve().parents[1]
        approvals: list[str] = []
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(project_root),
                CommandPolicy(project_root),
                lambda action, _detail: approvals.append(action) or True,
            )
        )
        config = AppConfig(
            workspace=project_root,
            provider=ProviderConfig("openai", "test-key", "https://example.test", "test"),
            extensions=ExtensionsConfig(enabled=True),
            mcp=MCPConfig(
                enabled=True,
                servers=(
                    MCPServerConfig(
                        "local_exit",
                        "stdio",
                        "python",
                        args=("tests/fixtures/fake_mcp_server.py", "--exit-immediately"),
                        enabled=True,
                    ),
                ),
            ),
        )

        async def operation(active_registry: ToolRegistry) -> None:
            self.assertFalse(active_registry.contains("mcp__local_exit__echo"))

        await asyncio.wait_for(
            run_mcp_task(
                config,
                registry,
                source_env={
                    "PATH": r"C:\\Windows\\System32",
                    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                    "SYSTEMROOT": r"C:\\Windows",
                },
                audit=None,
                cancellation=CancellationToken(),
                operation=operation,
            ),
            timeout=5.0,
        )
        self.assertEqual(["dangerous_mcp_server_start"], approvals)
        self.assertFalse(registry.contains("mcp__local_exit__echo"))


class MCPRuntimeSyncTests(unittest.TestCase):
    def test_sync_entry_uses_exactly_one_asyncio_run(self) -> None:
        async def sentinel() -> str:
            return "done"

        coroutine = sentinel()
        try:
            with mock.patch(
                "tricoder.mcp.runtime.run_mcp_task",
                new=mock.Mock(return_value=coroutine),
            ), mock.patch(
                "tricoder.mcp.runtime.asyncio.run",
                return_value="done",
            ) as run:
                result = run_mcp_task_sync(
                    mock.sentinel.config,
                    mock.sentinel.registry,
                    source_env={},
                    audit=None,
                    cancellation=CancellationToken(),
                    operation=mock.sentinel.operation,
                    manager_factory=mock.sentinel.factory,
                )
            self.assertEqual("done", result)
            run.assert_called_once_with(coroutine)
        finally:
            coroutine.close()
