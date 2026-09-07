import asyncio
import copy
import io
import json
import logging
import os
import sys
import time
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import anyio
from mcp.types import Tool as SDKTool


# 让 ``python -m unittest`` 在未安装包的源码工作树中也能直接发现 ``src``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.mcp.client import (
    _DEFERRED_REAP_TASKS,
    MCPClient,
    MCPClientError,
    MCPCleanupError,
    MCPProtocolError,
    MCPTimeoutError,
)
from tricoder.mcp.models import MCPCallResult, MCPServerState, MCPToolSpec
from tricoder.mcp.sdk import MCPSDK, SDKLogSource, isolate_sdk_logs, load_mcp_sdk
from tricoder.mcp.security import MCPLaunchRequest
from tricoder.mcp.transport import (
    MCPProcessExitEvidence, MCPTransportOutcome, VerifiedStdioTransport,
)
from tests.test_mcp_transport import ControlledClock, ControlledProcess, ProcessHarness


FAKE_SECRET = "fake-secret-must-not-leak"
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class _RecordingContext:
    """只模拟异步上下文协议，不启动进程或网络。"""

    def __init__(
        self,
        name,
        value,
        events,
        *,
        enter_error=None,
        enter_gate=None,
        exit_error=None,
        exit_delay=0.0,
        exit_ignore_cancellation_for=0.0,
        require_same_task=False,
    ):
        self.name = name
        self.value = value
        self.events = events
        self.enter_error = enter_error
        self.enter_gate = enter_gate
        self.exit_error = exit_error
        self.exit_delay = exit_delay
        self.exit_ignore_cancellation_for = exit_ignore_cancellation_for
        self.require_same_task = require_same_task
        self.enter_task = None

    async def __aenter__(self):
        self.events.append(f"{self.name}.enter")
        self.enter_task = asyncio.current_task()
        if self.enter_gate is not None:
            await self.enter_gate.wait()
        if self.enter_error is not None:
            raise self.enter_error
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append(f"{self.name}.exit")
        if self.require_same_task and asyncio.current_task() is not self.enter_task:
            raise RuntimeError("context exited from a different task")
        if self.exit_delay:
            await asyncio.sleep(self.exit_delay)
        if self.exit_ignore_cancellation_for:
            await _sleep_ignoring_cancellation(self.exit_ignore_cancellation_for)
        if self.exit_error is not None:
            raise self.exit_error
        return False


class _FakeSession:
    def __init__(self, events):
        self.events = events
        self.initialize_calls = 0
        self.list_calls = 0
        self.call_calls = []
        self.initialize_error = None
        self.list_error = None
        self.call_error = None
        self.initialize_gate = None
        self.list_gate = None
        self.call_gate = None
        self.list_ignore_cancellation_for = 0.0
        self.call_ignore_cancellation_for = 0.0
        self.list_late_error = None
        self.call_late_error = None
        self.cancelled_operations = []
        self.delayed_cancellation_completed = []
        self.tools = [
            SimpleNamespace(
                name="Echo Tool",
                description="返回文本",
                inputSchema=copy.deepcopy(INPUT_SCHEMA),
            )
        ]
        self.call_result = {
            "content": [{"type": "text", "text": "safe text"}],
            "isError": False,
        }

    async def initialize(self):
        self.events.append("initialize")
        self.initialize_calls += 1
        await self._complete("initialize", self.initialize_gate, self.initialize_error)
        return SimpleNamespace()

    async def list_tools(self):
        self.events.append("list_tools")
        self.list_calls += 1
        await self._complete("list_tools", self.list_gate, self.list_error)
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.events.append("call_tool")
        self.call_calls.append((name, arguments))
        await self._complete("call_tool", self.call_gate, self.call_error)
        if "nested" in arguments:
            arguments["nested"]["value"] = "server-mutated"
        return self.call_result

    async def _complete(self, name, gate, error):
        try:
            if gate is not None:
                await gate.wait()
            if error is not None:
                raise error
        except asyncio.CancelledError:
            self.cancelled_operations.append(name)
            delay = (
                self.call_ignore_cancellation_for
                if name == "call_tool"
                else self.list_ignore_cancellation_for
                if name == "list_tools"
                else 0.0
            )
            if delay:
                await _sleep_ignoring_cancellation(delay)
                self.delayed_cancellation_completed.append(name)
                late_error = (
                    self.call_late_error
                    if name == "call_tool"
                    else self.list_late_error
                    if name == "list_tools"
                    else None
                )
                if late_error is not None:
                    raise late_error
            raise


class _FakeSDKHarness:
    def __init__(self):
        self.events = []
        self.parameters = None
        self.errlog = None
        self.session = _FakeSession(self.events)
        self.transport_enter_error = None
        self.transport_enter_gate = None
        self.transport_exit_error = None
        self.session_enter_error = None
        self.session_enter_gate = None
        self.session_exit_error = None
        self.session_exit_delay = 0.0
        self.session_exit_ignore_cancellation_for = 0.0
        self.session_streams = None
        self.loader_calls = 0
        self.require_same_task = False
        self.stdio_bindings = ProcessHarness(ControlledProcess()).bindings()
        self.transport_on_close = None
        self.transport_exit_evidence = MCPProcessExitEvidence.VERIFIED
        self.transport_resources_closed = True
        self.transports = []

    def sdk_loader(self):
        self.loader_calls += 1
        return MCPSDK(
            client_session=self.client_session,
            stdio_server_parameters=self.stdio_server_parameters,
            stdio_bindings=self.stdio_bindings,
            log_sources=load_mcp_sdk().log_sources,
        )

    def stdio_server_parameters(self, **kwargs):
        self.parameters = kwargs
        return SimpleNamespace(**kwargs)

    def transport_factory(self, parameters, *, errlog, bindings):
        self.errlog = errlog
        assert bindings is self.stdio_bindings
        transport = FakeVerifiedStdioTransport(self)
        self.transports.append(transport)
        return transport

    def client_session(self, read_stream, write_stream):
        self.session_streams = (read_stream, write_stream)
        return _RecordingContext(
            "session",
            self.session,
            self.events,
            enter_error=self.session_enter_error,
            enter_gate=self.session_enter_gate,
            exit_error=self.session_exit_error,
            exit_delay=self.session_exit_delay,
            exit_ignore_cancellation_for=self.session_exit_ignore_cancellation_for,
            require_same_task=self.require_same_task,
        )


class FakeVerifiedStdioTransport(_RecordingContext):
    """只有受控关闭路径完成后才发布验证成功；异常或取消保留未知证据。"""

    def __init__(self, harness):
        self.harness = harness
        self._outcome = MCPTransportOutcome(MCPProcessExitEvidence.NOT_STARTED, False)
        super().__init__(
            "transport",
            ("read-stream", "write-stream"),
            harness.events,
            enter_error=harness.transport_enter_error,
            enter_gate=harness.transport_enter_gate,
            exit_error=harness.transport_exit_error,
            require_same_task=harness.require_same_task,
        )

    @property
    def outcome(self):
        return self._outcome

    async def __aenter__(self):
        try:
            streams = await super().__aenter__()
        except BaseException:
            # 此替身在 enter 成功前没有分配真实资源；明确区别于真实 partial allocation。
            self._outcome = MCPTransportOutcome(MCPProcessExitEvidence.NOT_STARTED, True)
            raise
        self._outcome = MCPTransportOutcome(MCPProcessExitEvidence.UNKNOWN, False)
        return streams

    async def __aexit__(self, exc_type, exc, traceback):
        if self.harness.transport_on_close is not None:
            self.harness.transport_on_close()
        await super().__aexit__(exc_type, exc, traceback)
        self._outcome = MCPTransportOutcome(
            self.harness.transport_exit_evidence,
            self.harness.transport_resources_closed,
        )


class _ControlledSDKHarness(_FakeSDKHarness):
    """真实本地 transport 配合内存进程，只替换不在本轮范围的协议 session。"""

    def __init__(self, process=None):
        super().__init__()
        self.process_harness = ProcessHarness(process or ControlledProcess())

    def sdk_loader(self):
        bindings = self.process_harness.bindings()
        return MCPSDK(
            client_session=self.client_session,
            stdio_server_parameters=self.stdio_server_parameters,
            stdio_bindings=bindings,
            log_sources=load_mcp_sdk().log_sources,
        )

    def transport_factory(self, parameters, *, errlog, bindings):
        self.errlog = errlog
        transport = VerifiedStdioTransport(parameters, errlog=errlog, bindings=bindings)
        self.transports.append(transport)
        return transport


class _IdleSDKPipe:
    """内存管道：仅在明确关闭后结束读取，不访问外部程序。"""

    def __init__(self):
        self.closed = asyncio.Event()

    async def send(self, data):
        return None

    async def receive(self, max_bytes=65_536):
        await self.closed.wait()
        raise anyio.EndOfStream

    async def aclose(self):
        self.closed.set()


class _IdleSDKProcess:
    """拒绝随 stdin 关闭而退出，迫使真实 SDK 走终止及回收流程。"""

    def __init__(self):
        self.stdin = _IdleSDKPipe()
        self.stdout = _IdleSDKPipe()
        self.pid = 12345
        self.returncode = None
        self.terminated = False
        self.job_closed = False
        self.transport_closed = False
        self._process = SimpleNamespace(_transport=self)

    def close(self):
        self.transport_closed = True


async def _sleep_ignoring_cancellation(duration):
    """测试专用：在有限时间内吞掉重复取消，用于验证真实墙钟上界。"""

    deadline = asyncio.get_running_loop().time() + duration
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            continue


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = _FakeSDKHarness()
        self.request = MCPLaunchRequest(
            command=str(Path.cwd() / "trusted-python.exe"),
            args=("-m", "fake_server"),
            cwd=Path.cwd(),
            env={"SAFE_NAME": FAKE_SECRET},
            approval_detail="approved",
        )

    def make_client(self, **kwargs):
        return MCPClient(
            "docs",
            self.request,
            initialize_timeout=kwargs.pop("initialize_timeout", 0.2),
            operation_timeout=kwargs.pop("operation_timeout", 0.2),
            cleanup_timeout=kwargs.pop("cleanup_timeout", 0.2),
            sdk_loader=self.harness.sdk_loader,
            transport_factory=self.harness.transport_factory,
            **kwargs,
        )

    async def wait_for_deferred_reap_consumption(
        self,
        loop_errors: list[object],
        *,
        timeout: float = 1.5,
    ) -> None:
        """仅为测试等待 deferred-reap done callback 消费晚到异常。"""

        deadline = asyncio.get_running_loop().time() + timeout
        while _DEFERRED_REAP_TASKS:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(
                    "timed out waiting for deferred reap tasks to be consumed; "
                    f"remaining={len(_DEFERRED_REAP_TASKS)} "
                    f"loop_errors={len(loop_errors)}"
                )
            await asyncio.sleep(0.01)
        self.assertEqual([], loop_errors)

    def use_controlled_process(self, process=None, *, cleanup_timeout=1.0):
        clock = ControlledClock()
        self.enterContext(patch("tricoder.mcp.transport.monotonic", clock.monotonic))
        self.enterContext(patch("tricoder.mcp.transport.sleep", clock.sleep))
        self.harness = _ControlledSDKHarness(process)
        return self.make_client(initialize_timeout=2.0, cleanup_timeout=cleanup_timeout)

    async def test_unknown_process_exit_keeps_client_failed_across_repeated_stop(self):
        """进程退出无法证明时，owner 正常完成也不得把 stop 判作成功。"""
        client = self.use_controlled_process()
        await client.start(CancellationToken())
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, self.harness.transports[0].outcome.process_exit)
        self.assertTrue(self.harness.transports[0].outcome.resources_closed)
        for operation in (client.list_tools(CancellationToken()), client.call_tool("echo", {}, CancellationToken())):
            with self.assertRaisesRegex(MCPClientError, "^mcp_client_not_ready$"):
                await operation

    async def test_verified_nonzero_exit_allows_client_stopped(self):
        """非零退出码仍然是已退出证据，不应误报清理失败。"""
        process = ControlledProcess()
        client = self.use_controlled_process(process)
        await client.start(CancellationToken())
        process.returncode = 7
        await client.stop()
        self.assertEqual(MCPServerState.STOPPED, client.state)
        self.assertEqual(MCPTransportOutcome(MCPProcessExitEvidence.VERIFIED, True), self.harness.transports[0].outcome)

    async def test_two_clients_do_not_share_cleanup_evidence(self):
        """一个进程退出未知不能污染另一个已验证的 client，也不能反向清除失败。"""
        first = self.use_controlled_process()
        first_harness = self.harness
        second = self.use_controlled_process(ControlledProcess(exit_on="terminate"))
        await asyncio.gather(first.start(CancellationToken()), second.start(CancellationToken()))
        results = await asyncio.gather(first.stop(), second.stop(), return_exceptions=True)
        self.assertIsInstance(results[0], MCPCleanupError)
        self.assertIsNone(results[1])
        self.assertEqual(MCPServerState.FAILED, first.state)
        self.assertEqual(MCPServerState.STOPPED, second.state)
        self.assertIsNot(first_harness.transports[0], self.harness.transports[0])
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await first.stop()

    async def test_unknown_cleanup_does_not_replace_native_stop_cancellation(self):
        """取消 stop 的主异常保持原生取消，未知证据仍由后续 stop 暴露。"""
        client = self.use_controlled_process()
        self.harness.session_exit_delay = 0.03
        await client.start(CancellationToken())
        stopping = asyncio.create_task(client.stop())
        while "session.exit" not in self.harness.events:
            await asyncio.sleep(0)
        stopping.cancel("native-stop-cancel")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await stopping
        self.assertEqual(("native-stop-cancel",), caught.exception.args)
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()

    async def test_unknown_cleanup_preserves_initialize_native_cancellation_identity(self):
        """session 的原生取消也必须保持对象身份，不能被未知退出证据覆盖。"""
        client = self.use_controlled_process()
        primary = asyncio.CancelledError("initialize-cancel")
        self.harness.session.initialize_error = primary
        with self.assertRaises(asyncio.CancelledError) as caught:
            await client.start(CancellationToken())
        self.assertIs(primary, caught.exception)
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()

    async def check_repeated_stop_cancellation(self, *, during_reap):
        """只观察真实等待抛出的取消对象，transport、退出证据和收割逻辑均真实运行。"""
        from tricoder.mcp import client as client_module

        client = self.use_controlled_process(cleanup_timeout=0.05 if during_reap else 1.0)
        if during_reap:
            self.harness.session_exit_ignore_cancellation_for = 0.2
        else:
            self.harness.session_exit_delay = 0.08
        await client.start(CancellationToken())
        owner = client._lifecycle_task
        first_observed = asyncio.Event()
        reap_entered = asyncio.Event()
        cancellations = []
        wait_timeouts = []
        stopping = None
        original_wait_for = asyncio.wait_for
        original_reap = client_module._reap_task

        async def observe_wait_for(operation, timeout):
            is_stopper = asyncio.current_task() is stopping
            if is_stopper:
                wait_timeouts.append(timeout)
            try:
                return await original_wait_for(operation, timeout)
            except asyncio.CancelledError as cancellation:
                if is_stopper:
                    cancellations.append(cancellation)
                    first_observed.set()
                raise

        async def observe_reap(task):
            if task is owner and asyncio.current_task() is stopping:
                reap_entered.set()
            return await original_reap(task)

        with (
            patch("tricoder.mcp.client.asyncio.wait_for", observe_wait_for),
            patch("tricoder.mcp.client._reap_task", observe_reap),
        ):
            stopping = asyncio.create_task(client.stop())
            try:
                while "session.exit" not in self.harness.events:
                    await asyncio.sleep(0)
                await asyncio.sleep(0.02)
                stopping.cancel("first-primary-cancel")
                await original_wait_for(first_observed.wait(), 1.0)
                if during_reap:
                    await original_wait_for(reap_entered.wait(), 1.0)
                stopping.cancel("second-cancel")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await stopping
                if during_reap:
                    self.assertIn(owner, _DEFERRED_REAP_TASKS)
            finally:
                await asyncio.gather(stopping, owner, return_exceptions=True)

        self.assertEqual(MCPServerState.FAILED, client.state)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, self.harness.transports[0].outcome.process_exit)
        self.assertNotIn(owner, _DEFERRED_REAP_TASKS)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()
        self.assertEqual(("first-primary-cancel",), caught.exception.args)
        self.assertIs(cancellations[0], caught.exception)
        self.assertLess(wait_timeouts[1], wait_timeouts[0] - 0.01)

    async def test_repeated_cancel_during_unknown_cleanup_preserves_first_identity(self):
        await self.check_repeated_stop_cancellation(during_reap=False)

    async def test_repeated_cancel_during_owner_reap_preserves_first_identity(self):
        await self.check_repeated_stop_cancellation(during_reap=True)

    async def test_inconsistent_exit_or_unclosed_resources_never_allow_stopped(self):
        """进入成功后，NOT_STARTED 或未关闭资源均不得伪造 STOPPED。"""
        for evidence, closed in (
            (MCPProcessExitEvidence.NOT_STARTED, True),
            (MCPProcessExitEvidence.VERIFIED, False),
        ):
            with self.subTest(evidence=evidence, closed=closed):
                self.harness = _FakeSDKHarness()
                self.harness.transport_exit_evidence = evidence
                self.harness.transport_resources_closed = closed
                client = self.make_client()
                await client.start(CancellationToken())
                for _ in range(2):
                    with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
                        await client.stop()
                self.assertEqual(MCPServerState.FAILED, client.state)

    async def test_partial_transport_enter_keeps_unknown_process_failure_sticky(self):
        """transport 已创建进程后进入失败，stack 未登记它也不能丢失证据。"""
        process = ControlledProcess()
        process.stdin = None
        client = self.use_controlled_process(process)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.start(CancellationToken())
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()

    async def test_start_uses_exact_parameters_real_null_sink_and_initializes_once(self):
        """防止启动参数漂移、stderr 被捕获进内存或重复 initialize。"""
        client = self.make_client()

        await client.start(CancellationToken())

        self.assertEqual(MCPServerState.READY, client.state)
        self.assertEqual(1, self.harness.session.initialize_calls)
        self.assertEqual(
            {
                "command": self.request.command,
                "args": list(self.request.args),
                "env": dict(self.request.env),
                "cwd": str(self.request.cwd),
                "encoding_error_handler": "replace",
            },
            self.harness.parameters,
        )
        self.assertEqual(("read-stream", "write-stream"), self.harness.session_streams)
        self.assertIsInstance(self.harness.errlog, io.TextIOBase)
        self.assertNotIsInstance(self.harness.errlog, io.StringIO)
        self.assertEqual(os.devnull, self.harness.errlog.name)
        self.assertFalse(self.harness.errlog.closed)
        self.assertEqual(
            ["transport.enter", "session.enter", "initialize"],
            self.harness.events,
        )

        await client.stop()
        self.assertTrue(self.harness.errlog.closed)

    async def test_list_tools_returns_validated_sdk_independent_immutable_specs(self):
        """防止 SDK 对象、原始 Schema 可变引用或未验证 Schema 越过边界。"""
        client = self.make_client()
        await client.start(CancellationToken())

        specs = await client.list_tools(CancellationToken())
        self.harness.session.tools[0].inputSchema["properties"]["text"]["type"] = "integer"

        self.assertIsInstance(specs, tuple)
        self.assertEqual(1, len(specs))
        self.assertIsInstance(specs[0], MCPToolSpec)
        self.assertEqual("Echo Tool", specs[0].raw_name)
        self.assertEqual("mcp__docs__echo_tool", specs[0].public_name)
        self.assertEqual("string", specs[0].input_schema["properties"]["text"]["type"])
        self.assertNotIsInstance(specs[0], SimpleNamespace)
        await client.stop()

    async def test_call_tool_copies_arguments_and_normalizes_result(self):
        """防止 server 修改调用方参数，或 SDK CallToolResult 泄漏到上层。"""
        client = self.make_client()
        await client.start(CancellationToken())
        arguments = {"nested": {"value": "original"}}

        result = await client.call_tool("raw.echo", arguments, CancellationToken())

        self.assertEqual(MCPCallResult(True, "safe text"), result)
        self.assertEqual("raw.echo", self.harness.session.call_calls[0][0])
        self.assertIsNot(arguments, self.harness.session.call_calls[0][1])
        self.assertEqual("original", arguments["nested"]["value"])
        await client.stop()

    async def test_initialize_timeout_cancels_and_awaits_operation_without_leaking_secret(self):
        """防止 initialize 超时后协程遗留，或错误文本泄漏环境值。"""
        self.harness.session.initialize_gate = asyncio.Event()
        client = self.make_client(initialize_timeout=0.01)

        with self.assertRaisesRegex(MCPTimeoutError, r"^mcp_initialize_timeout$") as caught:
            await client.start(CancellationToken())

        self.assertNotIn(FAKE_SECRET, str(caught.exception))
        self.assertIn("initialize", self.harness.session.cancelled_operations)
        self.assertEqual(MCPServerState.FAILED, client.state)
        self.assertEqual(
            ["transport.enter", "session.enter", "initialize", "session.exit", "transport.exit"],
            self.harness.events,
        )

    async def test_unrecoverable_cleanup_failure_overrides_start_timeout_safely(self):
        """防止无法确认资源回收时仍把启动超时误报为已经安全结束。"""
        self.harness.session.initialize_gate = asyncio.Event()
        self.harness.session_exit_error = RuntimeError(f"close raw {FAKE_SECRET}")
        client = self.make_client(initialize_timeout=0.01)

        with self.assertRaisesRegex(MCPCleanupError, r"^mcp_cleanup_failed$") as caught:
            await client.start(CancellationToken())

        self.assertNotIn(FAKE_SECRET, str(caught.exception))
        self.assertTrue(self.harness.errlog.closed)
        self.assertEqual(MCPServerState.FAILED, client.state)

    async def test_list_and_call_timeouts_use_distinct_fixed_categories(self):
        """防止不同请求超时混为底层 transport 文本或留下未收割任务。"""
        client = self.make_client(operation_timeout=0.01)
        await client.start(CancellationToken())

        self.harness.session.list_gate = asyncio.Event()
        with self.assertRaisesRegex(MCPTimeoutError, r"^mcp_list_tools_timeout$"):
            await client.list_tools(CancellationToken())
        self.assertIn("list_tools", self.harness.session.cancelled_operations)

        self.harness.session.call_gate = asyncio.Event()
        with self.assertRaisesRegex(MCPTimeoutError, r"^mcp_tool_timeout$"):
            await client.call_tool("raw.echo", {}, CancellationToken())
        self.assertIn("call_tool", self.harness.session.cancelled_operations)
        await client.stop()

    async def test_cancellation_before_start_does_not_load_sdk(self):
        """防止已取消任务仍创建 transport 或加载 SDK。"""
        token = CancellationToken()
        token.cancel()
        client = self.make_client()

        with self.assertRaises(CancellationError):
            await client.start(token)

        self.assertEqual(0, self.harness.loader_calls)
        self.assertEqual(MCPServerState.STOPPED, client.state)

    async def test_token_cancellation_bounds_transport_and_session_enter(self):
        """防止 transport/session enter 阻塞时 token 取消无法终止 start。"""
        for blocked_phase in ("transport", "session"):
            with self.subTest(blocked_phase=blocked_phase):
                self.harness = _FakeSDKHarness()
                self.harness.require_same_task = True
                gate = asyncio.Event()
                if blocked_phase == "transport":
                    self.harness.transport_enter_gate = gate
                else:
                    self.harness.session_enter_gate = gate
                client = self.make_client(initialize_timeout=1.0)
                token = CancellationToken()
                asyncio.get_running_loop().call_later(0.01, token.cancel)
                started = time.monotonic()

                with self.assertRaises(CancellationError):
                    await asyncio.wait_for(client.start(token), timeout=0.4)

                self.assertLess(time.monotonic() - started, 0.3)
                self.assertEqual(MCPServerState.FAILED, client.state)
                self.assertTrue(self.harness.errlog.closed)
                if blocked_phase == "session":
                    self.assertIn("transport.exit", self.harness.events)

    async def test_caller_task_cancellation_during_enter_cleans_in_owner_task(self):
        """防止直接 cancel start 调用者时跨 task 清理或遗留 owner。"""
        self.harness.require_same_task = True
        self.harness.session_enter_gate = asyncio.Event()
        client = self.make_client(initialize_timeout=1.0)
        start_task = asyncio.create_task(client.start(CancellationToken()))
        while "session.enter" not in self.harness.events:
            await asyncio.sleep(0)

        start_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await start_task

        self.assertEqual(MCPServerState.FAILED, client.state)
        self.assertTrue(self.harness.errlog.closed)
        self.assertIn("transport.exit", self.harness.events)

    async def test_native_cancellation_publishes_token_before_sdk_cleanup(self):
        """启动和请求的局部清理也必须先看见共享取消，而不等最外层 scope 收尾。"""
        for phase in ("start", "call"):
            with self.subTest(phase=phase):
                harness = _FakeSDKHarness()
                token = CancellationToken()
                entered = asyncio.Event()
                observed = []

                async def waiting(*args):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        if phase == "call":
                            observed.append(token.is_cancelled)

                if phase == "start":
                    harness.transport_on_close = lambda: observed.append(token.is_cancelled)
                    harness.session.initialize = waiting
                else:
                    harness.session.call_tool = waiting
                client = MCPClient("docs", self.request, sdk_loader=harness.sdk_loader, transport_factory=harness.transport_factory)
                if phase == "call":
                    await client.start(token)
                task = asyncio.create_task(
                    client.start(token) if phase == "start" else client.call_tool("echo", {}, token)
                )
                try:
                    await asyncio.wait_for(entered.wait(), 1.0)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual([True], observed)
                finally:
                    await client.stop()

    async def test_cancellation_during_call_propagates_and_reaps_operation(self):
        """防止运行中取消被包装成协议错误，或 call_tool task 残留。"""
        client = self.make_client(operation_timeout=1.0)
        await client.start(CancellationToken())
        self.harness.session.call_gate = asyncio.Event()
        token = CancellationToken()
        asyncio.get_running_loop().call_later(0.01, token.cancel)

        with self.assertRaises(CancellationError):
            await client.call_tool("raw.echo", {}, token)

        self.assertIn("call_tool", self.harness.session.cancelled_operations)
        self.assertEqual(MCPServerState.READY, client.state)
        await client.stop()

    async def test_operation_reap_budget_is_a_true_wall_clock_bound(self):
        """防止 wait_for 在协程吞取消后继续等待到伪装的 0.8 秒完成点。"""
        client = self.make_client(operation_timeout=0.01)
        await client.start(CancellationToken())
        self.harness.session.call_gate = asyncio.Event()
        self.harness.session.call_ignore_cancellation_for = 0.8
        started = time.monotonic()

        with self.assertRaisesRegex(MCPCleanupError, r"^mcp_cleanup_failed$"):
            await client.call_tool("raw.echo", {}, CancellationToken())

        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.45)
        self.assertLess(elapsed, 0.7)
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPClientError, r"^mcp_client_not_ready$"):
            await client.call_tool("raw.echo", {}, CancellationToken())
        await asyncio.sleep(0.35)
        self.assertIn("call_tool", self.harness.session.delayed_cancellation_completed)
        await client.stop()

    async def test_native_cancel_survives_deferred_operation_cleanup(self):
        """请求吞取消时，有限清理失败不得覆盖 runner 的原始 CancelledError。"""
        client = self.make_client(operation_timeout=2.0)
        await client.start(CancellationToken())
        self.harness.session.call_gate = asyncio.Event()
        self.harness.session.call_ignore_cancellation_for = 0.8
        token = CancellationToken()
        task = asyncio.create_task(client.call_tool("echo", {}, token))
        try:
            while "call_tool" not in self.harness.events:
                await asyncio.sleep(0)
            task.cancel("original-runner-cancel")
            try:
                await task
            except asyncio.CancelledError as exc:
                self.assertEqual(("original-runner-cancel",), exc.args)
            except MCPCleanupError:
                self.fail("有限回收错误覆盖了原始 runner 取消")
            else:
                self.fail("取消异常未传播")
            self.assertTrue(token.is_cancelled)
            self.assertEqual(MCPServerState.FAILED, client.state)
        finally:
            await self.wait_for_deferred_reap_consumption([])
            await client.stop()

    async def test_caller_cancel_during_reap_tracks_and_consumes_late_operation_error(self):
        """防止收割等待者被取消后，晚到 operation 异常成为未检索异常。"""

        client = self.make_client(operation_timeout=2.0)
        await client.start(CancellationToken())
        self.harness.session.call_gate = asyncio.Event()
        self.harness.session.call_ignore_cancellation_for = 0.8
        self.harness.session.call_late_error = RuntimeError(
            f"late raw {FAKE_SECRET}"
        )
        token = CancellationToken()
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            call_task = asyncio.create_task(
                client.call_tool("raw.echo", {}, token)
            )
            loop.call_later(0.01, token.cancel)
            loop.call_later(0.12, call_task.cancel)

            with self.assertRaises(asyncio.CancelledError):
                await call_task

            self.assertEqual(MCPServerState.FAILED, client.state)
            with self.assertRaisesRegex(MCPClientError, r"^mcp_client_not_ready$"):
                await client.list_tools(CancellationToken())
            self.assertGreaterEqual(len(_DEFERRED_REAP_TASKS), 1)
            await self.wait_for_deferred_reap_consumption(loop_errors)
            await client.stop()
        finally:
            if client._lifecycle_task is not None:
                await client.stop()
            loop.set_exception_handler(previous_handler)

    async def test_token_cancellation_survives_deferred_call_and_list_reaping(self):
        """防止延迟 operation 的 cleanup 状态覆盖 Task 6 依赖的显式取消。"""
        for operation_name in ("call_tool", "list_tools"):
            with self.subTest(operation_name=operation_name):
                self.harness = _FakeSDKHarness()
                client = self.make_client(operation_timeout=2.0)
                await client.start(CancellationToken())
                token = CancellationToken()
                loop_errors = []
                loop = asyncio.get_running_loop()
                previous_handler = loop.get_exception_handler()
                loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
                try:
                    if operation_name == "call_tool":
                        self.harness.session.call_gate = asyncio.Event()
                        self.harness.session.call_ignore_cancellation_for = 0.8
                        operation = client.call_tool("raw.echo", {}, token)
                    else:
                        self.harness.session.list_gate = asyncio.Event()
                        self.harness.session.list_ignore_cancellation_for = 0.8
                        operation = client.list_tools(token)
                    loop.call_later(0.01, token.cancel)
                    started = time.monotonic()

                    with self.assertRaisesRegex(CancellationError, r"^操作已取消$"):
                        await operation

                    elapsed = time.monotonic() - started
                    self.assertGreater(elapsed, 0.45)
                    self.assertLess(elapsed, 0.7)
                    self.assertEqual(MCPServerState.FAILED, client.state)
                    with self.assertRaisesRegex(MCPClientError, r"^mcp_client_not_ready$"):
                        await client.call_tool("raw.echo", {}, CancellationToken())
                    await asyncio.sleep(0.35)
                    self.assertIn(
                        operation_name,
                        self.harness.session.delayed_cancellation_completed,
                    )
                    self.assertEqual([], loop_errors)
                    await client.stop()
                    self.assertEqual(MCPServerState.STOPPED, client.state)
                    self.assertIsNone(client._lifecycle_task)
                    self.assertIsNone(client._stop_requested)
                finally:
                    if client._lifecycle_task is not None:
                        await client.stop()
                    loop.set_exception_handler(previous_handler)

    async def test_transport_and_protocol_errors_are_fixed_and_redacted(self):
        """防止 transport、SDK repr 或 server 原始异常穿透到 UI。"""
        self.harness.transport_enter_error = RuntimeError(
            f"transport raw {FAKE_SECRET} {self.harness!r}"
        )
        client = self.make_client()

        with self.assertRaisesRegex(MCPProtocolError, r"^mcp_transport_error$") as caught:
            await client.start(CancellationToken())
        self.assertNotIn(FAKE_SECRET, str(caught.exception))

        harness = _FakeSDKHarness()
        harness.session.initialize_error = ValueError(f"bad frame {FAKE_SECRET}")
        self.harness = harness
        client = self.make_client()
        with self.assertRaisesRegex(MCPProtocolError, r"^mcp_protocol_error$") as caught:
            await client.start(CancellationToken())
        self.assertNotIn(FAKE_SECRET, str(caught.exception))

    async def test_list_schema_error_and_call_protocol_error_are_fixed(self):
        """防止恶意 Schema 和工具异常正文直接返回给上层。"""
        client = self.make_client()
        await client.start(CancellationToken())
        self.harness.session.tools[0].inputSchema = {"type": "mystery"}

        with self.assertRaisesRegex(MCPProtocolError, r"^mcp_protocol_error$"):
            await client.list_tools(CancellationToken())

        self.harness.session.call_error = RuntimeError(f"tool raw {FAKE_SECRET}")
        with self.assertRaisesRegex(MCPProtocolError, r"^mcp_protocol_error$") as caught:
            await client.call_tool("raw.echo", {}, CancellationToken())
        self.assertNotIn(FAKE_SECRET, str(caught.exception))
        await client.stop()

    async def test_locked_parser_logs_are_isolated_without_muting_other_contexts(self):
        """真实 SDK 解析坏帧时，原文不能先于安全错误进入宿主日志。"""
        from mcp.client.stdio import _parse_line

        logger = logging.getLogger("mcp.client.stdio")
        host_logger = logging.getLogger("tricoder.test.host")
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        original_filters = tuple(logger.filters)
        original_level = logger.level
        logger.addHandler(handler)
        host_logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def initialize():
            parsed = _parse_line('MALFORMED-PROTOCOL-PRIVATE-SENTINEL')
            self.assertIsInstance(parsed, ValueError)
            # 不相关 logger 即使在同一个 task 中也必须照常输出。
            host_logger.warning("HOST-VISIBLE")
            entered.set()
            await release.wait()
            raise parsed

        self.harness.session.initialize = initialize
        client = self.make_client(initialize_timeout=2.0)
        start = asyncio.create_task(client.start(CancellationToken()))
        try:
            await asyncio.wait_for(entered.wait(), 1.0)
            # 来自另一个 task/线程的同名 SDK logger 不属于本连接，不能被静音。
            logger.warning("OTHER-TASK-VISIBLE")
            await asyncio.to_thread(logger.warning, "OTHER-THREAD-VISIBLE")
            release.set()
            with self.assertRaisesRegex(MCPProtocolError, "^mcp_protocol_error$"):
                await start
            self.assertNotIn("MALFORMED-PROTOCOL-PRIVATE-SENTINEL", capture.getvalue())
            self.assertNotIn("Failed to parse JSONRPC", capture.getvalue())
            for marker in ("HOST-VISIBLE", "OTHER-TASK-VISIBLE", "OTHER-THREAD-VISIBLE"):
                self.assertIn(marker, capture.getvalue())
            self.assertEqual(original_filters, tuple(logger.filters))
            self.assertEqual(logging.DEBUG, logger.level)
        finally:
            release.set()
            await asyncio.gather(start, return_exceptions=True)
            await client.stop()
            logger.removeHandler(handler)
            host_logger.removeHandler(handler)
            logger.setLevel(original_level)

    async def test_log_sources_remain_owned_until_deferred_request_finishes(self):
        """生命周期先关闭时，延迟结束的 SDK 请求仍须持有自己的日志租约。"""
        client = self.make_client(operation_timeout=0.01)
        late = asyncio.Event()

        async def call_tool(name, arguments):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await _sleep_ignoring_cancellation(0.7)
                await _deliver_locked_invalid_notification("LATE-REQUEST-SENTINEL")
                late.set()
                return {"content": []}

        self.harness.session.call_tool = call_tool
        from tricoder.mcp import sdk as sdk_module
        with _capture_sdk_logs() as captures:
            await client.start(CancellationToken())
            with self.assertRaises(MCPCleanupError):
                await client.call_tool("echo", {}, CancellationToken())
            await client.stop()
            self.assertTrue(client._log_sources)
            self.assertIn(sdk_module._SDK_LOG_FILTER, logging.getLogger("client").filters)
            await asyncio.wait_for(late.wait(), 1.0)
            await self.wait_for_deferred_reap_consumption([])
            self.assertEqual((), client._log_sources)
            self.assertNotIn(sdk_module._SDK_LOG_FILTER, logging.getLogger("client").filters)
            for capture in captures:
                self.assertNotIn("LATE-REQUEST-SENTINEL", capture.getvalue())

    async def test_deferred_sdk_cleanup_keeps_log_lease_until_helper_finishes(self):
        """transport 自持的终止 helper 可晚于失败清理 owner 结束，租约不能提前释放。"""
        from mcp.client.stdio import _parse_line
        from tricoder.mcp import sdk as sdk_module

        client = self.use_controlled_process()
        import importlib
        utilities = importlib.import_module(
            "mcp.os.win32.utilities" if sys.platform == "win32" else "mcp.os.posix.utilities"
        )
        late = asyncio.Event()

        async def terminate(process):
            await _sleep_ignoring_cancellation(0.2)
            _parse_line("DEFERRED-CLEANUP-SENTINEL")
            late.set()

        def loader():
            sdk = load_mcp_sdk()
            bindings = replace(
                self.harness.process_harness.bindings(),
                terminate_process_tree=sdk.stdio_bindings.terminate_process_tree,
            )
            return replace(sdk, client_session=self.harness.client_session, stdio_bindings=bindings)

        client._sdk_loader = loader
        with patch("tricoder.mcp.transport._TERMINATE_SECONDS", 0.01), \
             patch("tricoder.mcp.transport._TASK_REAP_SECONDS", 0.01), \
             patch.object(utilities, "terminate_windows_process_tree" if sys.platform == "win32" else "terminate_posix_process_tree", terminate), \
             _capture_sdk_logs() as captures:
            await client.start(CancellationToken())
            with self.assertRaises(MCPCleanupError):
                await client.stop()
            still_filtered = sdk_module._SDK_LOG_FILTER in logging.getLogger("mcp.client.stdio").filters
            await asyncio.wait_for(late.wait(), 1)
            await asyncio.sleep(0)
            self.assertTrue(still_filtered)
            for capture in captures:
                self.assertNotIn("DEFERRED-CLEANUP-SENTINEL", capture.getvalue())
            self.assertEqual((), client._log_sources)
            self.assertNotIn(sdk_module._SDK_LOG_FILTER, logging.getLogger("mcp.client.stdio").filters)

    async def test_source_scopes_cover_lifecycle_list_call_cleanup_and_cancellation(self):
        """真实通知日志在每条 SDK 边界均受保护，异常/取消不泄漏租约。"""
        from tricoder.mcp import sdk as sdk_module
        from mcp.client.stdio import _parse_line

        for mode in ("normal", "startup_failure", "native_cancel", "token_cancel", "cleanup_error"):
            with self.subTest(mode=mode), _capture_sdk_logs() as captures:
                self.harness = _FakeSDKHarness()
                client = self.make_client(initialize_timeout=1)
                token = CancellationToken()

                async def initialize():
                    await _deliver_locked_invalid_notification("CLIENT-INIT-SENTINEL")
                    if mode == "startup_failure":
                        raise RuntimeError("startup-controlled")
                    if mode == "native_cancel":
                        raise asyncio.CancelledError("native-controlled")
                    if mode == "token_cancel":
                        token.cancel()
                        await asyncio.Event().wait()

                async def list_tools():
                    await _deliver_locked_invalid_notification("CLIENT-LIST-SENTINEL")
                    return SimpleNamespace(tools=[])

                async def call_tool(name, arguments):
                    await _deliver_locked_invalid_notification("CLIENT-CALL-SENTINEL")
                    return {"content": []}

                def closing():
                    _parse_line("CLIENT-CLEANUP-SENTINEL")

                self.harness.session.initialize = initialize
                self.harness.session.list_tools = list_tools
                self.harness.session.call_tool = call_tool
                self.harness.transport_on_close = closing
                if mode == "cleanup_error":
                    self.harness.transport_exit_error = RuntimeError("cleanup-controlled")
                expected = {
                    "startup_failure": MCPProtocolError,
                    "native_cancel": asyncio.CancelledError,
                    "token_cancel": CancellationError,
                }.get(mode)
                if expected:
                    with self.assertRaises(expected):
                        await client.start(token)
                else:
                    await client.start(token)
                    self.assertEqual((), await client.list_tools(token))
                    await client.call_tool("echo", {}, token)
                if mode == "cleanup_error":
                    with self.assertRaises(MCPCleanupError):
                        await client.stop()
                else:
                    await client.stop()
                self.assertEqual((), client._log_sources)
                for source in load_mcp_sdk().log_sources:
                    self.assertNotIn(sdk_module._SDK_LOG_FILTER, logging.getLogger(source.logger_name).filters)
                for capture in captures:
                    self.assertNotIn("SENTINEL", capture.getvalue())

    async def test_sdk_log_boundary_survives_overlapping_clients_and_call_tasks(self):
        """先关闭一个连接不能撤销另一个连接及其独立请求 task 的日志保护。"""
        from mcp.client.stdio import _parse_line

        first = self.make_client()
        second_harness = _FakeSDKHarness()
        second = MCPClient("other", self.request, sdk_loader=second_harness.sdk_loader, transport_factory=second_harness.transport_factory)
        logger = logging.getLogger("mcp.client.stdio")
        original_filters = tuple(logger.filters)
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)

        async def call_tool(_name, _arguments):
            raise _parse_line("CALL-PROTOCOL-PRIVATE-SENTINEL")

        second_harness.session.call_tool = call_tool
        logger.addHandler(handler)
        try:
            await first.start(CancellationToken())
            await second.start(CancellationToken())
            await first.stop()
            with self.assertRaises(MCPProtocolError):
                await second.call_tool("echo", {}, CancellationToken())
            self.assertNotIn("CALL-PROTOCOL-PRIVATE-SENTINEL", capture.getvalue())
            await second.stop()
            self.assertEqual(original_filters, tuple(logger.filters))
        finally:
            await first.stop()
            await second.stop()
            logger.removeHandler(handler)

    async def test_list_tools_accepts_real_sdk_python_schema_field(self):
        """锁定 mcp 2.1.1 Tool 的 Python 字段名 input_schema 兼容边界。"""
        self.harness.session.tools = [
            SDKTool(name="echo", description=None, inputSchema=copy.deepcopy(INPUT_SCHEMA))
        ]
        client = self.make_client()
        await client.start(CancellationToken())

        specs = await client.list_tools(CancellationToken())

        self.assertEqual("echo", specs[0].raw_name)
        self.assertEqual("", specs[0].description)
        self.assertEqual("string", specs[0].input_schema["properties"]["text"]["type"])
        await client.stop()

    async def test_stop_exits_session_then_transport_and_is_idempotent(self):
        """防止关闭顺序反转或重复 stop 二次退出上下文。"""
        client = self.make_client()
        await client.start(CancellationToken())

        await client.stop()
        await client.stop()

        self.assertEqual(
            ["transport.enter", "session.enter", "initialize", "session.exit", "transport.exit"],
            self.harness.events,
        )
        self.assertEqual(MCPServerState.STOPPED, client.state)

    async def test_default_cleanup_allows_locked_sdk_to_terminate_and_reap_process(self):
        """外层默认预算不能在 SDK 的两秒自然退出等待结束前打断升级清理。"""
        await self._check_locked_sdk_process_cleanup(cancel_start=False)

    async def test_cancelled_start_waits_for_locked_sdk_process_cleanup(self):
        """启动取消不能在 SDK 尚持有活进程时仅收割 owner 半秒就返回。"""
        await self._check_locked_sdk_process_cleanup(cancel_start=True)

    async def _check_locked_sdk_process_cleanup(self, *, cancel_start):
        sdk = load_mcp_sdk()
        process = _IdleSDKProcess()
        initialize_entered = asyncio.Event()
        if cancel_start:
            async def initialize():
                initialize_entered.set()
                await asyncio.Event().wait()
            self.harness.session.initialize = initialize

        async def terminate(candidate):
            self.assertIs(process, candidate)
            process.terminated = True
            # 模拟终止后事件循环确认退出的时间；保持 SDK 的原始两秒宽限期。
            await asyncio.sleep(0.1)
            process.returncode = -1

        async def create_process(parameters, errlog):
            return process

        bindings = replace(
            sdk.stdio_bindings,
            create_process=create_process,
            terminate_process_tree=terminate,
            close_process_job=lambda _: setattr(process, "job_closed", True) or True,
        )
        client = MCPClient(
            "docs", self.request,
            sdk_loader=lambda: MCPSDK(
                self.harness.client_session, sdk.stdio_server_parameters, bindings, sdk.log_sources,
            ),
        )
        owner = None
        start = None
        with self.subTest(cancel_start=cancel_start):
            try:
                token = CancellationToken()
                if cancel_start:
                    start = asyncio.create_task(client.start(token))
                    await asyncio.wait_for(initialize_entered.wait(), 1.0)
                    owner = client._lifecycle_task
                    token.cancel()
                    with self.assertRaises(CancellationError):
                        await start
                else:
                    await client.start(token)
                    owner = client._lifecycle_task
                    try:
                        await client.stop()
                    except MCPCleanupError:
                        self.fail("默认预算在 SDK 进程回收前耗尽")
                self.assertTrue(process.terminated)
                self.assertEqual(-1, process.returncode)
                self.assertTrue(process.stdin.closed.is_set())
                self.assertTrue(process.stdout.closed.is_set())
                self.assertTrue(process.job_closed)
                self.assertTrue(process.transport_closed)
                self.assertIsNotNone(owner)
                self.assertTrue(owner.done())
            finally:
                # RED 时也释放内存管道，避免 shield 中的 drain task 污染后续测试。
                process.returncode = -1
                await process.stdin.aclose()
                await process.stdout.aclose()
                if owner is not None:
                    await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 2.0)
                if start is not None:
                    await asyncio.gather(start, return_exceptions=True)

    async def test_contexts_are_exited_by_the_same_task_that_entered_them(self):
        """防止官方 AnyIO cancel scope 因跨 task 退出而拒绝清理。"""
        self.harness.require_same_task = True
        client = self.make_client()

        await client.start(CancellationToken())
        await client.stop()

        self.assertEqual(MCPServerState.STOPPED, client.state)

    async def test_caller_cancellation_waits_for_shielded_cleanup_then_marks_stopped(self):
        """防止调用方取消 stop 时打断已开始的 session/transport 回收。"""
        self.harness.session_exit_delay = 0.02
        client = self.make_client()
        await client.start(CancellationToken())
        stop_task = asyncio.create_task(client.stop())
        while "session.exit" not in self.harness.events:
            await asyncio.sleep(0)

        stop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stop_task

        self.assertTrue(self.harness.errlog.closed)
        self.assertIn("transport.exit", self.harness.events)
        self.assertEqual(MCPServerState.STOPPED, client.state)

    async def test_repeated_caller_cancel_does_not_leave_completed_owner_references(self):
        """防止 stop 等待者连续取消后已完成 owner 长期滞留在 client。"""
        self.harness.session_exit_delay = 0.08
        client = self.make_client(cleanup_timeout=0.2)
        await client.start(CancellationToken())
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            stop_task = asyncio.create_task(client.stop())
            while "session.exit" not in self.harness.events:
                await asyncio.sleep(0)
            stop_task.cancel()
            await asyncio.sleep(0.02)
            stop_task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await stop_task
            await asyncio.sleep(0.12)

            self.assertEqual(MCPServerState.STOPPED, client.state)
            self.assertIsNone(client._lifecycle_task)
            self.assertIsNone(client._stop_requested)
            self.assertEqual([], loop_errors)
            await client.stop()
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_partial_start_failure_closes_entered_transport(self):
        """防止 session enter 失败时已经进入的 transport 泄漏。"""
        self.harness.session_enter_error = RuntimeError(f"session raw {FAKE_SECRET}")
        client = self.make_client()

        with self.assertRaisesRegex(MCPProtocolError, r"^mcp_transport_error$"):
            await client.start(CancellationToken())

        self.assertEqual(["transport.enter", "session.enter", "transport.exit"], self.harness.events)
        self.assertTrue(self.harness.errlog.closed)
        self.assertEqual(MCPServerState.FAILED, client.state)

    async def test_token_cancellation_remains_primary_when_context_cleanup_fails(self):
        """防止 cleanup 异常覆盖 Task 6 必须识别的显式 CancellationError。"""
        self.harness.session_enter_gate = asyncio.Event()
        self.harness.transport_exit_error = RuntimeError(f"close raw {FAKE_SECRET}")
        client = self.make_client(initialize_timeout=1.0)
        token = CancellationToken()
        asyncio.get_running_loop().call_later(0.01, token.cancel)

        with self.assertRaises(CancellationError) as caught:
            await asyncio.wait_for(client.start(token), timeout=0.4)

        self.assertNotIn(FAKE_SECRET, str(caught.exception))
        self.assertEqual(MCPServerState.FAILED, client.state)
        self.assertTrue(self.harness.errlog.closed)

    async def test_cleanup_failure_is_fixed_redacted_and_clears_references(self):
        """防止 close 异常正文泄漏，或失败后仍保留可调用 session。"""
        self.harness.session_exit_error = RuntimeError(f"close raw {FAKE_SECRET}")
        client = self.make_client()
        await client.start(CancellationToken())

        with self.assertRaisesRegex(MCPCleanupError, r"^mcp_cleanup_failed$") as caught:
            await client.stop()

        self.assertNotIn(FAKE_SECRET, str(caught.exception))
        self.assertTrue(self.harness.errlog.closed)
        self.assertIn("transport.exit", self.harness.events)
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPClientError, r"^mcp_client_not_ready$"):
            await client.list_tools(CancellationToken())
        with self.assertRaisesRegex(MCPCleanupError, r"^mcp_cleanup_failed$"):
            await client.stop()

    async def test_startup_cleanup_failure_remains_visible_without_owner(self):
        """失败启动完成后即使 owner 引用已释放，清理失败仍须阻止成功退出。"""
        self.harness.session.initialize_error = RuntimeError("initialize-private")
        self.harness.session_exit_error = RuntimeError("cleanup-private")
        client = self.make_client()
        with self.assertRaises(MCPCleanupError):
            await client.start(CancellationToken())
        self.assertIsNone(client._lifecycle_task)
        for _ in range(2):
            with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
                await client.stop()
        self.assertEqual(MCPServerState.FAILED, client.state)

    async def test_announced_ready_owner_cleanup_failure_is_not_forgotten(self):
        """非 stop 触发的生命周期失败不能被 done callback 消费后静默遗忘。"""
        self.harness.transport_exit_error = RuntimeError("cleanup-private")
        client = self.make_client()
        await client.start(CancellationToken())
        owner = client._lifecycle_task
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        self.assertIsNone(client._lifecycle_task)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()

    async def test_cleanup_timeout_cancels_close_and_returns_fixed_failure(self):
        """防止卡住的上下文退出让 stop 无限等待或遗留 stderr handle。"""
        self.harness.session_exit_delay = 1.0
        client = self.make_client(cleanup_timeout=0.01)
        await client.start(CancellationToken())

        with self.assertRaisesRegex(MCPCleanupError, r"^mcp_cleanup_failed$"):
            await client.stop()

        self.assertTrue(self.harness.errlog.closed)
        self.assertEqual(MCPServerState.FAILED, client.state)

    async def test_owner_reap_budget_is_a_true_wall_clock_bound(self):
        """防止 owner 吞取消时 stop 的 0.5 秒收割预算实际等待约 0.8 秒。"""
        self.harness.session_exit_ignore_cancellation_for = 0.8
        client = self.make_client(cleanup_timeout=0.01)
        await client.start(CancellationToken())
        started = time.monotonic()

        with self.assertRaisesRegex(MCPCleanupError, r"^mcp_cleanup_failed$"):
            await client.stop()

        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.45)
        self.assertLess(elapsed, 0.7)
        self.assertEqual(MCPServerState.FAILED, client.state)
        await asyncio.sleep(0.35)
        self.assertEqual(MCPServerState.FAILED, client.state)
        with self.assertRaisesRegex(MCPCleanupError, "^mcp_cleanup_failed$"):
            await client.stop()

    async def test_calls_before_start_fail_with_fixed_category(self):
        """防止未初始化 session 被意外解引用或暴露内部异常。"""
        client = self.make_client()

        with self.assertRaisesRegex(MCPClientError, r"^mcp_client_not_ready$"):
            await client.list_tools(CancellationToken())
        with self.assertRaisesRegex(MCPClientError, r"^mcp_client_not_ready$"):
            await client.call_tool("echo", {}, CancellationToken())

    async def test_real_stdio_fixture_replies_and_owner_task_is_reaped(self):
        """若真实 stdio lifecycle 没有关闭，捕获的 owner task 会留在当前事件循环。"""
        from tricoder.mcp.security import prepare_mcp_launch
        from tricoder.models import MCPServerConfig

        project_root = Path(__file__).resolve().parents[1]
        request = prepare_mcp_launch(
            MCPServerConfig(
                "local_test",
                "stdio",
                "python",
                args=("tests/fixtures/fake_mcp_server.py",),
                enabled=True,
            ),
            workspace=project_root,
            source_env={
                "PATH": r"C:\\Windows\\System32",
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "SYSTEMROOT": r"C:\\Windows",
            },
        )
        client = MCPClient(
            "local_test",
            request,
            initialize_timeout=2.0,
            operation_timeout=2.0,
            cleanup_timeout=2.0,
        )

        owner_task = None
        try:
            await asyncio.wait_for(client.start(CancellationToken()), timeout=3.0)
            owner_task = client._lifecycle_task
            self.assertIsNotNone(owner_task)
            result = await asyncio.wait_for(
                client.call_tool("echo", {"text": "client"}, CancellationToken()),
                timeout=3.0,
            )
            self.assertEqual("echo:client", result.text)
        finally:
            await asyncio.wait_for(client.stop(), timeout=3.0)

        assert owner_task is not None
        self.assertEqual(MCPServerState.STOPPED, client.state)
        self.assertIsNone(client._lifecycle_task)
        self.assertTrue(owner_task.done())
        self.assertNotIn(owner_task, asyncio.all_tasks())


@contextmanager
def _capture_sdk_logs():
    """在直接 handler、传播到 root 的 handler 和 stderr 捕获真实记录。"""
    names = (
        "client", "mcp.client.stdio", "mcp.shared.jsonrpc_dispatcher",
        "mcp.shared.dispatcher", "mcp.os.posix.utilities", "mcp.os.win32.utilities",
    )
    direct, root_output, stderr = io.StringIO(), io.StringIO(), io.StringIO()
    direct_handler = logging.StreamHandler(direct)
    root_handler = logging.StreamHandler(root_output)
    root = logging.getLogger()
    saved = []
    with redirect_stderr(stderr):
        stderr_handler = logging.StreamHandler()
        root.addHandler(root_handler)
        root.addHandler(stderr_handler)
        try:
            for name in names:
                logger = logging.getLogger(name)
                saved.append((logger, logger.level, logger.disabled, logger.propagate))
                logger.setLevel(logging.DEBUG)
                logger.disabled = False
                logger.propagate = True
                logger.addHandler(direct_handler)
            yield direct, root_output, stderr
        finally:
            for logger, level, disabled, propagate in saved:
                logger.removeHandler(direct_handler)
                logger.setLevel(level)
                logger.disabled = disabled
                logger.propagate = propagate
            root.removeHandler(root_handler)
            root.removeHandler(stderr_handler)


async def _deliver_locked_invalid_notification(sentinel):
    """真实 parser/session/dispatcher 发警告后丢弃通知，不启动外部 server。"""
    from mcp import ClientSession
    from mcp.client.stdio import _parse_line
    from mcp.shared.message import SessionMessage

    observed = []
    delivered = asyncio.Event()

    async def receive_notification(notification):
        observed.append(notification)
        delivered.set()

    incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](2)
    write, outgoing = anyio.create_memory_object_stream[SessionMessage](2)
    async with incoming, read, write, outgoing:
        async with ClientSession(read, write, message_handler=receive_notification):
            for progress in (sentinel, 1):
                message = _parse_line(json.dumps({
                    "jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": "offline-progress", "progress": progress},
                }))
                if not isinstance(message, SessionMessage):
                    raise AssertionError("synthetic notification must have a valid JSON-RPC envelope")
                await incoming.send(message)
            await asyncio.wait_for(delivered.wait(), 1.0)
    # 非法值只被丢弃，不应错误地断言其抛出协议异常。
    if len(observed) != 1 or observed[0].params.progress != 1:
        raise AssertionError("real SDK must drop invalid notification and deliver the valid one")


class SDKLogIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_final_lease_release_does_not_skip_inflight_host_filter(self):
        """末租约退出不能让旁路线程跳过原本会拒绝记录的宿主 filter。"""
        self._check_lease_change_during_bypass(install=False)

    def test_first_lease_install_does_not_repeat_inflight_host_filter(self):
        """首租约安装不能让宿主 filter 重复处理同一条在途记录。"""
        self._check_lease_change_during_bypass(install=True)

    def _check_lease_change_during_bypass(self, *, install):
        """只在线程 trace 中固定调度；真实 logger/filter/handler 流程不被替换。"""
        from tricoder.mcp import sdk as sdk_module

        logger = logging.getLogger("mcp.client.stdio")
        reached, resume = threading.Event(), threading.Event()
        observed, emitted, errors = [], [], []
        source = SDKLogSource(
            logger.name,
            r"C:\synthetic_sdk\stdio.py" if os.name == "nt" else "/synthetic_sdk/stdio.py",
        )

        class FirstHostFilter(logging.Filter):
            def filter(self, record):
                observed.append(("first", record.msg))
                if install:
                    record.msg += "|host"
                return install

        class SecondHostFilter(logging.Filter):
            def filter(self, record):
                observed.append(("second", record.msg))
                return True

        class Capture(logging.Handler):
            def emit(self, record):
                emitted.append(record.msg)

        first, second, capture = FirstHostFilter(), SecondHostFilter(), Capture()
        original_propagate, original_disabled = logger.propagate, logger.disabled
        original_filters = tuple(logger.filters)
        logger.addFilter(first)
        logger.addFilter(second)
        logger.addHandler(capture)
        logger.propagate = False
        logger.disabled = False

        def record():
            return logging.LogRecord(
                source.logger_name, logging.WARNING, source.source_path, 1,
                "BYPASS-SYNTHETIC", (), None,
            )

        expected_observed = (
            [("first", "BYPASS-SYNTHETIC"), ("second", "BYPASS-SYNTHETIC|host")]
            if install else [("first", "BYPASS-SYNTHETIC")]
        )
        expected_emitted = ["BYPASS-SYNTHETIC|host"] if install else []
        target = first if install else sdk_module._SDK_LOG_FILTER
        target_code = target.filter.__func__.__code__

        def trace(frame, event, arg):
            if (
                frame.f_code is target_code and frame.f_locals.get("self") is target
                and event == "return" and not reached.is_set()
            ):
                reached.set()
                if not resume.wait(3):
                    raise AssertionError("旁路线程调度等待超时")
            return trace

        def bypass():
            previous_trace = sys.gettrace()
            try:
                sys.settrace(trace)
                logger.handle(record())
            except BaseException as error:
                errors.append(error)
            finally:
                sys.settrace(previous_trace)

        thread = threading.Thread(target=bypass)
        started = False
        try:
            # 无 scope 对照先证明宿主的正常拦截/处理效果。
            logger.handle(record())
            self.assertEqual(expected_observed, observed)
            self.assertEqual(expected_emitted, emitted)
            observed.clear()
            emitted.clear()
            if install:
                thread.start()
                started = True
                self.assertTrue(reached.wait(3))
                with isolate_sdk_logs((source,)):
                    resume.set()
                    thread.join(3)
            else:
                with isolate_sdk_logs((source,)):
                    thread.start()
                    started = True
                    self.assertTrue(reached.wait(3))
                resume.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(expected_observed, observed)
            self.assertEqual(expected_emitted, emitted)
            self.assertEqual((*original_filters, first, second), tuple(logger.filters))
        finally:
            resume.set()
            if started:
                thread.join(3)
            logger.removeFilter(first)
            logger.removeFilter(second)
            logger.removeHandler(capture)
            logger.propagate, logger.disabled = original_propagate, original_disabled

    async def test_locked_invalid_notification_logs_are_isolated(self):
        """缺少真实 client 来源时，通知验证值会泄漏到所有捕获端。"""
        load_mcp_sdk()
        sentinel = "INVALID-NOTIFY-SYNTHETIC-SENTINEL"
        with _capture_sdk_logs() as captures:
            await _deliver_locked_invalid_notification(sentinel)
            for capture in captures:
                self.assertIn(sentinel, capture.getvalue())
                capture.seek(0)
                capture.truncate(0)
            with isolate_sdk_logs(load_mcp_sdk().log_sources):
                await _deliver_locked_invalid_notification(sentinel)
            for capture in captures:
                self.assertNotIn(sentinel, capture.getvalue())

    def test_sdk_log_source_filter_preserves_same_name_third_party(self):
        """共享 logger 名称和 module 值不能证明记录来自 SDK。"""
        load_mcp_sdk()
        with _capture_sdk_logs() as captures:
            with isolate_sdk_logs(load_mcp_sdk().log_sources):
                for name in ("client", "mcp.client.stdio"):
                    record = logging.LogRecord(
                        name, logging.WARNING,
                        r"C:\third_party\session.py" if os.name == "nt" else "/third_party/session.py", 1,
                        "THIRD-PARTY-" + name, (), None,
                    )
                    self.assertEqual("session", record.module)
                    logging.getLogger(name).handle(record)
            for capture in captures:
                for name in ("client", "mcp.client.stdio"):
                    self.assertIn("THIRD-PARTY-" + name, capture.getvalue())

    async def test_sdk_log_boundary_covers_dispatcher_and_request_paths(self):
        """shared dispatcher intercept 与 JSON-RPC 请求路径也会产生日志。"""
        from mcp.shared.dispatcher import run_notify_intercept
        from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

        load_mcp_sdk()

        def broken_intercept(method, params):
            raise ValueError("INTERCEPT-SYNTHETIC-SENTINEL")

        with _capture_sdk_logs() as captures:
            with isolate_sdk_logs(load_mcp_sdk().log_sources):
                self.assertFalse(run_notify_intercept(broken_intercept, "notifications/progress", {}))
                read_send, read = anyio.create_memory_object_stream(1)
                write, write_read = anyio.create_memory_object_stream(1)
                async with read_send, read, write, write_read:
                    dispatcher = JSONRPCDispatcher(read, write)
                    await write.aclose()
                    await dispatcher.notify("CLOSED-DISPATCHER-SENTINEL", {})
            for capture in captures:
                self.assertNotIn("INTERCEPT-SYNTHETIC-SENTINEL", capture.getvalue())
                self.assertNotIn("CLOSED-DISPATCHER-SENTINEL", capture.getvalue())

    async def test_overlapping_log_scopes_restore_only_owned_state(self):
        """必须在宿主 filter 能格式化或保存记录之前拒绝 SDK 原始日志。"""
        from mcp.client.stdio import _parse_line

        load_mcp_sdk()
        logger = logging.getLogger("mcp.client.stdio")
        observed = []

        class HostFilter(logging.Filter):
            def filter(self, record):
                observed.append(record)
                return True

        host_filter = HostFilter()
        added_filter = HostFilter()
        original_filters = tuple(logger.filters)
        sources = load_mcp_sdk().log_sources
        logger.addFilter(host_filter)
        try:
            with isolate_sdk_logs(sources):
                logger.addFilter(added_filter)
                with isolate_sdk_logs(sources):
                    _parse_line("HOST-FILTER-PRIVATE-SENTINEL")
            self.assertEqual([], observed)
            self.assertEqual((*original_filters, host_filter, added_filter), tuple(logger.filters))
        finally:
            logger.removeFilter(host_filter)
            logger.removeFilter(added_filter)

    async def test_overlapping_scopes_keep_bypass_task_thread_and_logger_properties(self):
        """按 logger 计数和任务局部作用域不能静音并行的宿主工作。"""
        from mcp.client.stdio import _parse_line
        from tricoder.mcp import sdk as sdk_module

        sources = load_mcp_sdk().log_sources
        stdio_source = tuple(source for source in sources if source.logger_name == "mcp.client.stdio")
        all_loggers = [logging.getLogger(source.logger_name) for source in sources]
        all_loggers.append(logging.getLogger())

        def state(logger):
            return (logger.level, logger.disabled, tuple(logger.handlers), logger.propagate, tuple(logger.filters))

        first_entered, second_entered = asyncio.Event(), asyncio.Event()
        first_release, second_release = asyncio.Event(), asyncio.Event()

        async def owner(selected, entered, release):
            with isolate_sdk_logs(selected):
                entered.set()
                await release.wait()
                _parse_line("OVERLAPPING-PRIVATE-SENTINEL")

        with _capture_sdk_logs() as captures:
            initial = {logger: state(logger) for logger in all_loggers}
            first = asyncio.create_task(owner(sources, first_entered, first_release))
            second = asyncio.create_task(owner(stdio_source, second_entered, second_release))
            try:
                await asyncio.wait_for(first_entered.wait(), 1)
                await asyncio.wait_for(second_entered.wait(), 1)
                # 旁路位于两个作用域之外，但仍使用真实 SDK 来源产生日志。
                _parse_line("BYPASS-TASK-VISIBLE")
                thread = threading.Thread(target=_parse_line, args=("BYPASS-THREAD-VISIBLE",))
                thread.start()
                await asyncio.to_thread(thread.join, 1)
                self.assertFalse(thread.is_alive())
                first_release.set()
                await first
                self.assertNotIn(sdk_module._SDK_LOG_FILTER, logging.getLogger("client").filters)
                self.assertIs(sdk_module._SDK_LOG_FILTER, logging.getLogger("mcp.client.stdio").filters[0])
                second_release.set()
                await second
                for capture in captures:
                    self.assertNotIn("OVERLAPPING-PRIVATE-SENTINEL", capture.getvalue())
                    self.assertIn("BYPASS-TASK-VISIBLE", capture.getvalue())
                    self.assertIn("BYPASS-THREAD-VISIBLE", capture.getvalue())
                self.assertEqual(initial, {logger: state(logger) for logger in all_loggers})
            finally:
                first_release.set()
                second_release.set()
                await asyncio.gather(first, second, return_exceptions=True)

    def test_exact_filter_is_lexical_and_never_formats_or_reads_files(self):
        """词法等价路径匹配，但名称前缀、相对路径和正文不能代替精确来源。"""
        from tricoder.mcp import sdk as sdk_module

        sources = load_mcp_sdk().log_sources
        source = next(source for source in sources if source.logger_name == "client")
        equivalent = str(Path(source.source_path).parent / "unused" / ".." / "session.py")
        cases = (
            ("client", equivalent, False),
            ("client", source.source_path + ".other", True),
            ("client.child", source.source_path, True),
            ("client", "session.py", True),
        )
        with isolate_sdk_logs(sources):
            with patch("pathlib.Path.resolve", side_effect=AssertionError("no resolve")), \
                 patch("os.stat", side_effect=AssertionError("no stat")), \
                 patch("os.getcwd", side_effect=AssertionError("no getcwd")), \
                 patch.object(logging.LogRecord, "getMessage", side_effect=AssertionError("no formatting")):
                for name, pathname, expected in cases:
                    record = logging.LogRecord(name, logging.WARNING, pathname, 1, object(), (), None)
                    self.assertEqual(expected, sdk_module._SDK_LOG_FILTER.filter(record))
        self.assertTrue(sdk_module._SDK_LOG_FILTER.filter(
            logging.LogRecord("client", logging.WARNING, source.source_path, 1, object(), (), None)
        ))

    async def test_scope_restores_after_exception_and_native_cancellation(self):
        """异常与原生取消退出必须精确释放各自租约。"""
        from tricoder.mcp import sdk as sdk_module

        sources = load_mcp_sdk().log_sources
        for error in (RuntimeError("controlled"), asyncio.CancelledError("controlled")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with isolate_sdk_logs(sources):
                        await _deliver_locked_invalid_notification("EXCEPTION-SENTINEL")
                        raise error
                for source in sources:
                    self.assertNotIn(sdk_module._SDK_LOG_FILTER, logging.getLogger(source.logger_name).filters)


if __name__ == "__main__":
    unittest.main()
