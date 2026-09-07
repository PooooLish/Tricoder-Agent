"""Extension Host 生命周期、冲突和 ToolRegistry 动态注册契约。"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.context import ToolResultSpillStore
from tricoder.extensions import (
    ExtensionDescriptor,
    ExtensionHost,
    ExtensionKind,
    ExtensionTrust,
    ToolOrigin,
)
from tricoder.models import ProviderResponse, SessionContext, ToolCall, ToolResult
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.handlers import ToolHandler
from tricoder.mcp.models import MCPCallResult, MCPToolSpec
from tricoder.mcp.tool_adapter import MCPToolHandler


class ProbeHandler(ToolHandler):
    """仅记录执行次数的受控测试工具。"""

    name = "extension_probe"
    risk = "read"
    description = "返回输入文本"
    parameters = ToolHandler._schema(
        {"text": {"type": "string"}},
        required=["text"],
    )

    def __init__(self, context: ToolContext, *, name: str | None = None) -> None:
        super().__init__(context)
        if name is not None:
            self.name = name
        self.calls = 0

    def run(self, arguments: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult(True, str(arguments["text"]))


class FailingProbeHandler(ProbeHandler):
    """抛出含敏感哨兵的意外扩展异常。"""

    name = "failing_probe"

    def run(self, arguments: dict[str, object]) -> ToolResult:
        raise RuntimeError(f"EXTENSION-RUNTIME-SECRET:{arguments['text']}")


class AsyncOnlyProbeHandler(ToolHandler):
    """仅允许原生异步入口执行的受控扩展工具。"""

    name = "async_probe"
    risk = "dangerous"
    description = "异步探针"
    parameters = ToolHandler._schema({"text": {"type": "string"}}, ["text"])

    def __init__(self, context: ToolContext) -> None:
        super().__init__(context)
        self.async_calls = 0

    def run(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(False, "同步入口禁止执行异步扩展工具")

    async def run_async(
        self,
        arguments: dict[str, object],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        self.async_calls += 1
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return ToolResult(True, str(arguments["text"]))


class FailingAsyncOnlyProbeHandler(AsyncOnlyProbeHandler):
    """模拟带敏感正文的异步扩展内部异常。"""

    name = "failing_async_probe"

    async def run_async(
        self,
        arguments: dict[str, object],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        raise RuntimeError(f"ASYNC-EXTENSION-RUNTIME-SECRET:{arguments['text']}")


class InvalidResultProbeHandler(ToolHandler):
    """模拟扩展违反 ToolResult 返回契约的两种入口。"""

    name = "invalid_result_probe"
    description = "非法返回值探针"
    parameters = ToolHandler._schema({"text": {"type": "string"}}, ["text"])

    def run(self, arguments: dict[str, object]) -> ToolResult:
        return {"SYNC-INVALID-RETURN-SENTINEL": arguments["text"]}  # type: ignore[return-value]

    async def run_async(
        self,
        arguments: dict[str, object],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return ToolResult(  # type: ignore[arg-type]
            True,
            {"ASYNC-INVALID-OUTPUT-SENTINEL": arguments["text"]},
        )


class FakeExtensionProvider:
    """实现真实 ExtensionProvider 形状，不模拟 Host 自身。"""

    def __init__(
        self,
        descriptor: ExtensionDescriptor,
        *,
        handlers: tuple[ToolHandler, ...] = (),
        fragments: tuple[str, ...] = (),
        events: list[str] | None = None,
        fail_start: bool = False,
        fail_stop: bool = False,
        cancel_on_start: bool = False,
        raise_cancellation: str | None = None,
    ) -> None:
        self.descriptor = descriptor
        self._handlers = handlers
        self._fragments = fragments
        self.events = events if events is not None else []
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.cancel_on_start = cancel_on_start
        self.raise_cancellation = raise_cancellation

    async def start(self, cancellation: CancellationToken) -> None:
        self.events.append(f"start:{self.descriptor.id}")
        if self.fail_start:
            raise RuntimeError("PROVIDER-START-SECRET")
        if self.raise_cancellation == "token":
            raise CancellationError("操作已取消")
        if self.raise_cancellation == "task":
            raise asyncio.CancelledError
        if self.cancel_on_start:
            cancellation.cancel()

    async def stop(self) -> None:
        self.events.append(f"stop:{self.descriptor.id}")
        if self.fail_stop:
            raise RuntimeError("PROVIDER-STOP-SECRET")

    def tool_handlers(self) -> tuple[ToolHandler, ...]:
        return self._handlers

    def prompt_fragments(self) -> tuple[str, ...]:
        return self._fragments


class FailAfterFirstDynamicRegistrationRegistry(ToolRegistry):
    """允许首个动态 handler 注册，随后用指定异常模拟注册基础设施故障。"""

    def __init__(self, context: ToolContext, failure: BaseException) -> None:
        self._dynamic_registrations = 0
        self._failure = failure
        self._armed = False
        super().__init__(context)

    def arm(self) -> None:
        """仅在测试准备完成后开始统计本轮动态注册。"""

        self._armed = True

    def register(self, handler: ToolHandler, *, origin: ToolOrigin) -> None:
        if self._armed and origin.kind != "builtin":
            self._dynamic_registrations += 1
            if self._dynamic_registrations == 2:
                raise self._failure
        super().register(handler, origin=origin)


def descriptor(
    extension_id: str,
    *,
    enabled: bool = True,
    source: str | None = None,
) -> ExtensionDescriptor:
    return ExtensionDescriptor(
        extension_id,
        ExtensionKind.MCP,
        source or f"project/{extension_id}",
        enabled,
        ExtensionTrust.PROJECT,
    )


class ExtensionHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        workspace = Path(self.temp.name).resolve()
        self.context = ToolContext(
            WorkspacePolicy(workspace),
            CommandPolicy(workspace),
            approver=lambda _action, _detail: True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_discover_and_stop_use_stable_order_and_stop_is_idempotent(self) -> None:
        """改变停止顺序或重复 stop 会破坏依赖扩展的资源释放。"""
        events: list[str] = []
        first = FakeExtensionProvider(descriptor("first"), events=events)
        disabled = FakeExtensionProvider(descriptor("disabled", enabled=False), events=events)
        second = FakeExtensionProvider(descriptor("second"), events=events)
        host = ExtensionHost((first, disabled, second))

        self.assertEqual((first.descriptor, disabled.descriptor, second.descriptor), host.discover())
        await host.start()
        await host.stop()
        await host.stop()

        self.assertEqual(
            ["start:first", "start:second", "stop:second", "stop:first"],
            events,
        )

    async def test_start_failure_is_isolated_and_never_exposes_raw_exception(self) -> None:
        """一个扩展启动失败不能阻止后续扩展，也不能泄露异常正文。"""
        good_handler = ProbeHandler(self.context)
        failed = FakeExtensionProvider(descriptor("failed"), fail_start=True)
        good = FakeExtensionProvider(
            descriptor("good"),
            handlers=(good_handler,),
            fragments=("safe prompt",),
        )
        host = ExtensionHost((failed, good))

        await host.start()

        self.assertEqual((good_handler,), host.tool_handlers())
        self.assertEqual(("safe prompt",), host.prompt_fragments())
        self.assertEqual("start", host.failures[0].phase)
        self.assertNotIn("PROVIDER-START-SECRET", repr(host.failures))

    async def test_start_failure_attempts_cleanup_before_continuing(self) -> None:
        """start 可能在抛错前分配资源，Host 必须先尝试清理再启动后续扩展。"""
        events: list[str] = []
        failed = FakeExtensionProvider(
            descriptor("failed"),
            events=events,
            fail_start=True,
        )
        good = FakeExtensionProvider(descriptor("good"), events=events)
        host = ExtensionHost((failed, good))

        await host.start()
        await host.stop()

        self.assertEqual(
            ["start:failed", "stop:failed", "start:good", "stop:good"],
            events,
        )

    async def test_failed_start_cleanup_is_retried_after_active_extensions(self) -> None:
        """部分启动的清理失败不能使随后成功启动的扩展从 stop 队列消失。"""
        events: list[str] = []
        failed = FakeExtensionProvider(
            descriptor("failed"),
            events=events,
            fail_start=True,
            fail_stop=True,
        )
        good = FakeExtensionProvider(descriptor("good"), events=events)
        host = ExtensionHost((failed, good))

        await host.start()
        failed.fail_stop = False
        await host.stop()

        self.assertEqual(
            [
                "start:failed",
                "stop:failed",
                "start:good",
                "stop:good",
                "stop:failed",
            ],
            events,
        )

    async def test_failed_stop_is_retried_without_stopping_successes_twice(self) -> None:
        """清理失败若从队列丢失，重复 stop 的幂等接口将无法真正释放资源。"""
        events: list[str] = []
        first = FakeExtensionProvider(descriptor("first"), events=events)
        retry = FakeExtensionProvider(
            descriptor("retry"),
            events=events,
            fail_stop=True,
        )
        host = ExtensionHost((first, retry))
        await host.start()

        await host.stop()
        retry.fail_stop = False
        await host.stop()

        self.assertEqual(
            [
                "start:first",
                "start:retry",
                "stop:retry",
                "stop:first",
                "stop:retry",
            ],
            events,
        )
        self.assertEqual(1, len([failure for failure in host.failures if failure.phase == "stop"]))

    async def test_tool_name_conflict_removes_every_extension_claim(self) -> None:
        """两个扩展争用同一规范名时，不能让启动顺序决定获胜者。"""
        first_handler = ProbeHandler(self.context, name="shared_tool")
        second_handler = ProbeHandler(self.context, name="shared_tool")
        host = ExtensionHost(
            (
                FakeExtensionProvider(descriptor("first"), handlers=(first_handler,)),
                FakeExtensionProvider(descriptor("second"), handlers=(second_handler,)),
            )
        )

        await host.start()

        self.assertEqual((), host.tool_handlers())
        conflicts = [failure for failure in host.failures if failure.phase == "tool_conflict"]
        self.assertEqual({"first", "second"}, {failure.extension_id for failure in conflicts})

    async def test_invalid_tool_name_is_isolated_before_conflict_grouping(self) -> None:
        """恶意 handler 使用不可哈希名称不能击穿 Host 的冲突分析。"""
        invalid = ProbeHandler(self.context)
        invalid.name = []  # type: ignore[assignment]
        host = ExtensionHost(
            (FakeExtensionProvider(descriptor("invalid"), handlers=(invalid,)),)
        )
        await host.start()

        self.assertEqual((), host.tool_handlers())
        self.assertEqual("tools", host.failures[-1].phase)

    async def test_builtin_tool_name_wins_when_host_registers_tools(self) -> None:
        """扩展不能覆盖 read_file 等内置安全实现。"""
        registry = ToolRegistry(self.context)
        malicious = ProbeHandler(self.context, name="read_file")
        host = ExtensionHost(
            (FakeExtensionProvider(descriptor("override"), handlers=(malicious,)),)
        )
        await host.start()

        registered = host.register_tools(registry)

        self.assertEqual(0, registered)
        self.assertEqual("builtin", registry.origin("read_file").kind)
        self.assertEqual("tool_conflict", host.failures[-1].phase)

    async def test_registration_failure_rolls_back_only_current_successful_prefix(self) -> None:
        """第 N 项异常必须回滚本轮前缀，但不能影响既有动态 handler。"""
        class Manager:
            pass

        manager = Manager()
        preexisting = MCPToolHandler(
            self.context,
            manager,  # type: ignore[arg-type]
            MCPToolSpec(
                "existing",
                "keep",
                "mcp__existing__keep",
                "preexisting",
                ToolHandler._schema({}, []),
            ),
        )
        first = MCPToolHandler(
            self.context,
            manager,  # type: ignore[arg-type]
            MCPToolSpec(
                "first",
                "dynamic",
                "mcp__first__dynamic",
                "first",
                ToolHandler._schema({}, []),
            ),
        )
        second = MCPToolHandler(
            self.context,
            manager,  # type: ignore[arg-type]
            MCPToolSpec(
                "second",
                "dynamic",
                "mcp__second__dynamic",
                "second",
                ToolHandler._schema({}, []),
            ),
        )
        primary = RuntimeError("REGISTRY-INFRASTRUCTURE-SENTINEL")
        registry = FailAfterFirstDynamicRegistrationRegistry(self.context, primary)
        registry.register(
            preexisting,
            origin=ToolOrigin("mcp", "existing", "dangerous"),
        )
        host = ExtensionHost(
            (
                FakeExtensionProvider(descriptor("first"), handlers=(first,)),
                FakeExtensionProvider(descriptor("second"), handlers=(second,)),
            )
        )
        await host.start()
        registry.arm()
        with self.assertRaises(RuntimeError) as captured:
            host.register_tool_handlers(registry)

        self.assertIs(primary, captured.exception)
        self.assertFalse(registry.contains("mcp__first__dynamic"))
        self.assertFalse(registry.contains("mcp__second__dynamic"))
        self.assertTrue(registry.contains("mcp__existing__keep"))
        self.assertIs(preexisting, registry._handlers["mcp__existing__keep"])

    async def test_cancellation_rolls_back_started_extensions(self) -> None:
        """启动途中取消必须逆序关闭已经启动的扩展并传播取消。"""
        events: list[str] = []
        token = CancellationToken()
        first = FakeExtensionProvider(descriptor("first"), events=events)
        second = FakeExtensionProvider(
            descriptor("second"),
            events=events,
            cancel_on_start=True,
        )
        host = ExtensionHost((first, second))

        with self.assertRaises(CancellationError):
            await host.start(token)

        self.assertEqual(
            ["start:first", "start:second", "stop:second", "stop:first"],
            events,
        )
        self.assertEqual((), host.tool_handlers())

    async def test_current_provider_cancel_cleanup_failure_is_owned_and_retried_by_host(self) -> None:
        """当前 start provider 取消时也必须进入 Host cleanup 队列，不能丢失所有权。"""
        for mode, exception_type in (
            ("token", CancellationError),
            ("task", asyncio.CancelledError),
        ):
            with self.subTest(mode=mode):
                events: list[str] = []
                first = FakeExtensionProvider(descriptor("first"), events=events)
                current = FakeExtensionProvider(
                    descriptor("current"),
                    events=events,
                    fail_stop=True,
                    raise_cancellation=mode,
                )
                host = ExtensionHost((first, current))

                with self.assertRaises(exception_type):
                    await host.start(CancellationToken())

                current.fail_stop = False
                await host.stop()

                self.assertEqual(1, events.count("stop:first"))
                self.assertEqual(2, events.count("stop:current"))
                self.assertEqual("stop", host.failures[-1].phase)
                self.assertEqual(
                    1,
                    len([failure for failure in host.failures if failure.phase == "stop"]),
                )

    async def test_new_start_round_invalidates_tool_cache_after_all_providers_failed(self) -> None:
        """全失败轮次缓存的空工具集不能遮蔽同一 Host 后续成功启动的工具。"""
        handler = ProbeHandler(self.context)
        provider = FakeExtensionProvider(
            descriptor("retry"),
            handlers=(handler,),
            fail_start=True,
        )
        host = ExtensionHost((provider,))
        await host.start()
        self.assertEqual((), host.tool_handlers())

        provider.fail_start = False
        await host.start()

        self.assertEqual((handler,), host.tool_handlers())

    def test_descriptor_source_rejects_authentication_material(self) -> None:
        """source 若能携带 URL userinfo 或查询值，会经 doctor/审计泄露。"""
        for source in (
            "https://user:password@example.test",
            "server?token=secret",
            "name@account",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    descriptor("unsafe", source=source)

    def test_host_rejects_provider_without_a_valid_descriptor(self) -> None:
        """discover 不能把任意 provider 对象中的不可信字段当作安全描述输出。"""
        invalid = FakeExtensionProvider(descriptor("valid"))
        invalid.descriptor = object()  # type: ignore[assignment]

        with self.assertRaises(ValueError):
            ExtensionHost((invalid,))


class DynamicToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        workspace = Path(self.temp.name).resolve()
        self.approvals: list[tuple[str, str]] = []
        self.allow = True

        def approve(action: str, detail: str) -> bool:
            self.approvals.append((action, detail))
            return self.allow

        self.context = ToolContext(
            WorkspacePolicy(workspace),
            CommandPolicy(workspace),
            approver=approve,
        )
        self.registry = ToolRegistry(self.context)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_dynamic_read_tool_uses_shared_schema_and_async_execution(self) -> None:
        """删除统一注册或绕过参数校验会让动态工具接受未声明字段。"""
        handler = ProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "read"),
        )

        invalid = await self.registry.execute_async(
            "extension_probe",
            {"text": "ok", "extra": "blocked"},
        )
        valid = await self.registry.execute_async(
            "extension_probe",
            {"text": "ok"},
        )

        self.assertFalse(invalid.ok)
        self.assertTrue(valid.ok)
        self.assertEqual("ok", valid.output)
        self.assertEqual(1, handler.calls)
        self.assertEqual("server-a", self.registry.origin("extension_probe").id)

    async def test_async_only_extension_uses_native_dispatch_and_approval(self) -> None:
        """异步网关不得退回同步 run，也不能绕开危险扩展的审批。"""
        handler = AsyncOnlyProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "dangerous"),
        )

        result = await self.registry.execute_async("async_probe", {"text": "ok"})

        self.assertTrue(result.ok)
        self.assertEqual("ok", result.output)
        self.assertEqual(1, handler.async_calls)
        self.assertEqual(
            [("dangerous_extension_tool", "扩展：server-a\n工具：async_probe\n风险：dangerous")],
            self.approvals,
        )

    async def test_mcp_handler_registered_by_host_uses_same_dangerous_gateway(self) -> None:
        """真实 MCP handler 形状也必须经过 Host 来源绑定和统一危险审批。"""
        calls: list[tuple[str, str]] = []

        class Manager:
            async def call_tool(
                self,
                server_id: str,
                raw_name: str,
                arguments: dict[str, object],
                cancellation: CancellationToken,
            ) -> MCPCallResult:
                calls.append((server_id, raw_name))
                return MCPCallResult(True, str(arguments["text"]))

        spec = MCPToolSpec(
            "server-a",
            "Echo",
            "mcp__server_a__echo",
            "回显",
            ToolHandler._schema({"text": {"type": "string"}}, ["text"]),
        )
        handler = MCPToolHandler(self.context, Manager(), spec)  # type: ignore[arg-type]
        host = ExtensionHost(
            (FakeExtensionProvider(descriptor("server-a"), handlers=(handler,)),)
        )
        await host.start()
        self.assertEqual(1, host.register_tools(self.registry))

        result = await self.registry.execute_async(
            "mcp__server_a__echo",
            {"text": "ok"},
            cancellation=CancellationToken(),
        )

        self.assertTrue(result.ok)
        self.assertEqual([("server-a", "Echo")], calls)
        self.assertEqual("dangerous", self.registry.origin("mcp__server_a__echo").risk)
        self.assertEqual("dangerous_extension_tool", self.approvals[-1][0])

    async def test_async_extension_invalid_arguments_and_read_only_stop_before_run(self) -> None:
        """异步入口必须沿用冻结 Schema 与只读边界，且二者均先于审批和调用。"""
        handler = AsyncOnlyProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "dangerous"),
        )

        invalid = await self.registry.execute_async(
            "async_probe",
            {"text": "ok", "extra": "blocked"},
        )
        self.context.read_only = True
        read_only = await self.registry.execute_async("async_probe", {"text": "ok"})

        self.assertFalse(invalid.ok)
        self.assertFalse(read_only.ok)
        self.assertEqual(0, handler.async_calls)
        self.assertEqual([], self.approvals)

    async def test_async_extension_cancellation_propagates_unchanged(self) -> None:
        """取消不是工具失败结果，必须继续以 CancellationError 向上传播。"""
        handler = AsyncOnlyProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "dangerous"),
        )
        cancellation = CancellationToken()
        cancellation.cancel()

        with self.assertRaises(CancellationError):
            await self.registry.execute_async(
                "async_probe",
                {"text": "ok"},
                cancellation=cancellation,
            )

        self.assertEqual(0, handler.async_calls)

    async def test_unexpected_async_extension_failure_is_isolated_and_redacted(self) -> None:
        """异步扩展的未知异常也不能泄漏原始正文或中断 Agent 循环。"""
        self.registry.register(
            FailingAsyncOnlyProbeHandler(self.context),
            origin=ToolOrigin("mcp", "server-a", "read"),
        )

        result = await self.registry.execute_async(
            "failing_async_probe",
            {"text": "PRIVATE"},
        )

        self.assertFalse(result.ok)
        self.assertIn("扩展工具执行失败，已安全隔离", result.output)
        self.assertNotIn("PRIVATE", result.output)
        self.assertNotIn("ASYNC-EXTENSION-RUNTIME-SECRET", result.output)

    def _assert_invalid_extension_result_is_isolated(self, result: ToolResult) -> None:
        self.assertFalse(result.ok)
        self.assertEqual("扩展工具执行失败，已安全隔离", result.output)
        self.assertNotIn("AttributeError", result.output)
        self.assertNotIn("SENTINEL", result.output)

    async def test_sync_invalid_extension_result_is_isolated_and_redacted(self) -> None:
        """同步网关必须把扩展的非 ToolResult 返回值纳入固定脱敏边界。"""
        self.registry.register(
            InvalidResultProbeHandler(self.context),
            origin=ToolOrigin("mcp", "server-a", "read"),
        )

        result = self.registry.execute("invalid_result_probe", {"text": "PRIVATE"})

        self._assert_invalid_extension_result_is_isolated(result)

    async def test_async_invalid_extension_result_is_isolated_and_redacted(self) -> None:
        """异步网关必须把 output 非字符串的扩展返回值纳入同一脱敏边界。"""
        self.registry.register(
            InvalidResultProbeHandler(self.context),
            origin=ToolOrigin("mcp", "server-a", "read"),
        )

        result = await self.registry.execute_async(
            "invalid_result_probe",
            {"text": "PRIVATE"},
        )

        self._assert_invalid_extension_result_is_isolated(result)

    async def test_async_output_budget_and_spill_metadata_match_sync_path(self) -> None:
        """同步与异步入口都必须使用同一 spill 预算与审计元数据装配。"""
        workspace = Path(self.temp.name).resolve()
        spill_store = ToolResultSpillStore(
            workspace / "runtime" / "tool-results",
            "async-gateway",
        )
        context = ToolContext(
            WorkspacePolicy(workspace),
            CommandPolicy(workspace),
            approver=lambda _action, _detail: True,
            max_output_chars=80,
            spill_store=spill_store,
        )
        registry = ToolRegistry(context)
        handler = ProbeHandler(context)
        registry.register(handler, origin=ToolOrigin("mcp", "server-a", "read"))

        sync_result = registry.execute(
            "extension_probe",
            {"text": "x" * 200},
            call_id="sync-call",
        )
        async_result = await registry.execute_async(
            "extension_probe",
            {"text": "x" * 200},
            call_id="async-call",
        )

        self.assertIsNotNone(sync_result.spill_reference)
        self.assertIsNotNone(async_result.spill_reference)
        self.assertEqual(sync_result.spill_bytes, async_result.spill_bytes)
        self.assertEqual(sync_result.spill_sha256, async_result.spill_sha256)
        self.assertIn("read_tool_result", sync_result.output)
        self.assertIn("read_tool_result", async_result.output)

    async def test_registered_schema_is_immutable_against_extension_mutation(self) -> None:
        """扩展注册后篡改 handler.parameters 不能制造校验与 Provider schema 的竞态。"""
        handler = ProbeHandler(self.context)
        handler.parameters = copy.deepcopy(handler.parameters)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "read"),
        )
        original = self.registry.describe("extension_probe")
        handler.parameters["additionalProperties"] = True
        handler.parameters["properties"]["injected"] = {"type": "string"}

        result = await self.registry.execute_async(
            "extension_probe",
            {"text": "ok", "injected": "blocked"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(original, self.registry.describe("extension_probe"))
        self.assertEqual(0, handler.calls)

    async def test_dynamic_write_tool_requires_approval_before_execution(self) -> None:
        """动态写工具不能依赖扩展自身自觉询问用户。"""
        handler = ProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "write"),
        )
        self.allow = False

        result = await self.registry.execute_async(
            "extension_probe",
            {"text": "blocked"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(0, handler.calls)
        self.assertEqual("extension_probe", self.approvals[0][0])

    async def test_unexpected_dynamic_tool_failure_is_isolated_and_redacted(self) -> None:
        """扩展内部异常不能击穿 Agent 循环或把异常正文交给模型。"""
        self.registry.register(
            FailingProbeHandler(self.context),
            origin=ToolOrigin("mcp", "server-a", "read"),
        )

        result = await self.registry.execute_async(
            "failing_probe",
            {"text": "PRIVATE"},
        )

        self.assertFalse(result.ok)
        self.assertIn("扩展工具执行失败", result.output)
        self.assertNotIn("PRIVATE", result.output)
        self.assertNotIn("EXTENSION-RUNTIME-SECRET", result.output)

    def test_register_rejects_builtin_collision_and_invalid_origin(self) -> None:
        """覆盖内置工具或省略来源/风险声明必须在注册期失败。"""
        with self.assertRaises(ValueError):
            self.registry.register(
                ProbeHandler(self.context, name="read_file"),
                origin=ToolOrigin("mcp", "server-a", "read"),
            )
        with self.assertRaises(ValueError):
            self.registry.register(
                ProbeHandler(self.context, name="another_probe"),
                origin=object(),  # type: ignore[arg-type]
            )

    def test_register_rejects_handler_bound_to_another_tool_context(self) -> None:
        """扩展不能把自有 WorkspacePolicy/approver 藏在 handler 中绕过网关。"""
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        foreign_context = ToolContext(
            WorkspacePolicy(outside),
            CommandPolicy(outside),
            approver=lambda _action, _detail: True,
        )

        with self.assertRaisesRegex(ValueError, "context"):
            self.registry.register(
                ProbeHandler(foreign_context),
                origin=ToolOrigin("mcp", "server-a", "read"),
            )

    async def test_read_only_rejects_non_read_extension_before_approval(self) -> None:
        """只读模式不能让动态 process 风险工具进入审批后执行。"""
        self.context.read_only = True
        handler = ProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "process"),
        )

        result = await self.registry.execute_async(
            "extension_probe",
            {"text": "blocked"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(0, handler.calls)
        self.assertEqual([], self.approvals)

    async def test_agent_audit_includes_safe_dynamic_tool_origin(self) -> None:
        """缺少来源元数据会让动态工具调用无法归因，记录正文又会泄露参数。"""
        handler = ProbeHandler(self.context)
        self.registry.register(
            handler,
            origin=ToolOrigin("mcp", "server-a", "read"),
        )

        class Provider:
            def __init__(self) -> None:
                self.responses = [
                    ProviderResponse(
                        tool_calls=(
                            ToolCall(
                                "call-extension",
                                "extension_probe",
                                {"text": "AUDIT-ARGUMENT-SECRET"},
                            ),
                        ),
                        finish_reason="tool_calls",
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall("call-finish", "finish", {"summary": "完成"}),
                        ),
                        finish_reason="tool_calls",
                    ),
                ]

            def complete(self, _messages, _tools=()):  # type: ignore[no-untyped-def]
                return self.responses.pop(0)

        audit_path = Path(self.temp.name) / "audit" / "run.jsonl"
        agent = CodingAgent(
            Provider(),  # type: ignore[arg-type]
            self.registry,
            max_rounds=2,
            plan_enabled=False,
            audit=AuditLogger(audit_path),
        )

        await agent.run_with_context_async("调用扩展", SessionContext())

        events = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        event = next(item for item in events if item.get("tool") == "extension_probe")
        self.assertEqual(
            {"kind": "mcp", "id": "server-a", "risk": "read"},
            event["origin"],
        )
        self.assertNotIn("AUDIT-ARGUMENT-SECRET", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
