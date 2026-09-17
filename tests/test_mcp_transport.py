import asyncio
import io
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import patch

from tricoder.mcp.transport import (
    MCPProcessExitEvidence,
    MCPStdioBindings,
    MCPTransportOutcome,
    VerifiedStdioTransport,
)


class ClosedStream(Exception):
    pass


class MemoryPipe:
    """以队列模拟可关闭管道，不启动操作系统进程。"""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False
        self.sent = []
        self.received = asyncio.Event()

    async def send(self, value):
        if self.closed:
            raise ClosedStream()
        self.sent.append(value)
        self.queue.put_nowait(value)
        self.received.set()

    async def receive(self):
        value = await self.queue.get()
        if value is _EOF:
            raise ClosedStream()
        return value

    async def aclose(self):
        self.closed = True
        self.queue.put_nowait(_EOF)


_EOF = object()


class MemoryEndpoint:
    def __init__(self, pipe):
        self.pipe = pipe
        self.closed = False

    async def send(self, value):
        if self.closed:
            raise ClosedStream()
        await self.pipe.send(value)

    async def receive(self):
        if self.closed:
            raise ClosedStream()
        return await self.pipe.receive()

    async def aclose(self):
        self.closed = True
        self.pipe.queue.put_nowait(_EOF)


class ControlledClock:
    """推进本地轮询时钟，同时给实际 asyncio 任务一次调度机会。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


class ControlledProcess:
    def __init__(self, *, exit_on=None):
        self.pid = 12345
        self.returncode = None
        self.stdin = MemoryPipe()
        self.stdout = MemoryPipe()
        self.exit_on = exit_on
        self.signals = []
        self.job_closed = False
        self.transport_closed = False

    def terminate(self):
        self.signals.append("terminate")
        if self.exit_on == "terminate":
            self.returncode = -15

    def kill(self):
        self.signals.append("kill")
        if self.exit_on == "kill":
            self.returncode = -9


class ProcessHarness:
    def __init__(self, process):
        self.process = process
        self.endpoints = []
        self.created = False
        self.termination_started = asyncio.Event()
        self.hold_termination = False

    async def create(self, parameters, errlog):
        self.created = True
        return self.process

    async def terminate(self, process):
        self.termination_started.set()
        if self.hold_termination:
            await asyncio.Event().wait()
        process.terminate()
        if process.returncode is None:
            process.kill()

    def close_job(self, process):
        process.job_closed = True
        return True

    def close_transport(self, process):
        process.transport_closed = True
        return True

    def streams(self, capacity):
        pipe = MemoryPipe()
        endpoints = (MemoryEndpoint(pipe), MemoryEndpoint(pipe))
        self.endpoints.extend(endpoints)
        return endpoints

    def parse(self, line):
        try:
            return SimpleNamespace(message=json.loads(line))
        except ValueError as exc:
            return exc

    def bindings(self):
        return MCPStdioBindings(
            create_process=self.create,
            terminate_process_tree=self.terminate,
            close_process_job=self.close_job,
            close_subprocess_transport=self.close_transport,
            create_memory_object_stream=self.streams,
            parse_message=self.parse,
            closed_resource_errors=(ClosedStream,),
            end_of_stream_errors=(ClosedStream,),
        )


class _EventDrivenSDKHarness:
    """仅供本模块端到端取消用例使用，避免反向导入另一测试模块。"""

    def __init__(self, process):
        self.process_harness = ProcessHarness(process)
        self.session = _EventDrivenSession()
        self.transports = []

    def sdk_loader(self):
        from tricoder.mcp.sdk import MCPSDK

        return MCPSDK(
            client_session=self.client_session,
            stdio_server_parameters=self.stdio_server_parameters,
            stdio_bindings=self.process_harness.bindings(),
            log_sources=(),
        )

    @staticmethod
    def stdio_server_parameters(**kwargs):
        return SimpleNamespace(**kwargs)

    def transport_factory(self, parameters, *, errlog, bindings):
        transport = VerifiedStdioTransport(parameters, errlog=errlog, bindings=bindings)
        self.transports.append(transport)
        return transport

    def client_session(self, _read_stream, _write_stream):
        return _EventDrivenSessionContext(self.session)


class _EventDrivenSession:
    """协议面最小替身；具体方法可由测试替换为受控阻塞点。"""

    async def initialize(self):
        return None

    async def call_tool(self, _name, _arguments):
        return SimpleNamespace(content=[], isError=False)


class _EventDrivenSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class VerifiedStdioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_command_then_mcp_share_absolute_deadline_without_cancellation(self):
        from tricoder import subprocess_control as control
        from tricoder.task_cleanup import TaskCleanup, cleanup_scope
        from unittest import mock
        from pathlib import Path
        for phase in ("timeout", "output_limit", "business_error"):
            with self.subTest(phase=phase):
                clock = ControlledClock()
                scope = TaskCleanup()
                budgets = []
                process = mock.Mock(returncode=None)
                process.poll.side_effect = lambda: process.returncode
                process.stdout.read1.return_value = b"XX" if phase == "output_limit" else b""
                process.stderr.read1.return_value = b""

                def terminate(_process, _env, _job, *, deadline):
                    budgets.append(deadline)
                    clock.now += 4.0
                    process.returncode = 0
                    return True

                def reader(*, target, args, daemon):
                    return SimpleNamespace(start=lambda: target(*args), is_alive=lambda: False)

                with patch.object(control, "time", SimpleNamespace(monotonic=clock.monotonic)), \
                     patch("tricoder.task_cleanup.time", SimpleNamespace(monotonic=clock.monotonic)), \
                     patch("tricoder.mcp.transport.monotonic", clock.monotonic), \
                     patch("tricoder.mcp.transport.sleep", clock.sleep), \
                     patch.object(control.threading, "Thread", side_effect=reader), \
                     patch.object(control, "_terminate_process_tree", side_effect=terminate), \
                     cleanup_scope(scope):
                    if phase == "business_error":
                        primary = ValueError("synthetic primary")
                        with patch.object(control.subprocess, "Popen", return_value=process), \
                             patch.object(control, "_create_windows_job", return_value=mock.Mock()), \
                             patch.object(control, "_collect_bounded_process", side_effect=primary):
                            with self.assertRaises(ValueError) as caught:
                                control.run_bounded_process(["synthetic"], cwd=Path("."), env={},
                                                            timeout=1, max_output_bytes=1)
                            self.assertIs(primary, caught.exception)
                    else:
                        result = control._collect_bounded_process(process, env={}, timeout=0,
                            max_output_bytes=1, windows_job=None)
                        self.assertTrue(result.timed_out if phase == "timeout" else result.output_exceeded)
                    transport, _ = self.make_transport(ControlledProcess())
                    async with transport:
                        pass
                self.assertEqual([5.0], budgets)
                self.assertEqual(5.0, scope.started_deadline, "非取消命令终止后 MCP 不能新领 5 秒")
                self.assertLessEqual(clock.now, 5.0)

    async def test_successful_command_cleanup_does_not_expire_a_long_running_task(self):
        from tricoder import subprocess_control as control
        from tricoder.task_cleanup import TaskCleanup, cleanup_scope
        from unittest import mock
        clock = ControlledClock()
        scope = TaskCleanup()
        with patch.object(control, "time", SimpleNamespace(monotonic=clock.monotonic)), \
             patch.object(control, "_terminate_process_tree", return_value=True), cleanup_scope(scope):
            first = control._ProcessResources(mock.Mock(returncode=0), {})
            self.assertTrue(first.finish(None))
            self.assertIsNone(scope.started_deadline)
            clock.now = 100.0
            second = control._ProcessResources(mock.Mock(returncode=0), {})
            self.assertTrue(second.finish(None))
            self.assertEqual(105.0, second.deadline)
            self.assertIsNone(scope.started_deadline)

    async def test_two_transports_share_task_cleanup_deadline_and_owner(self):
        from tricoder.task_cleanup import TaskCleanup, cleanup_scope
        clock = ControlledClock()
        self.enterContext(patch("tricoder.mcp.transport.monotonic", clock.monotonic))
        self.enterContext(patch("tricoder.mcp.transport.sleep", clock.sleep))
        scope = TaskCleanup()
        # 本测试注入绝对任务期限；不是让每个 transport 重新获得预算。
        scope._deadline = 5.0
        transports = [self.make_transport(ControlledProcess())[0] for _ in range(2)]
        with cleanup_scope(scope):
            for transport in transports:
                async with transport:
                    pass
        self.assertLessEqual(clock.now, 5.0, "多个 MCP 资源重新获得清理预算")
        self.assertTrue(scope.failed)
        self.assertTrue(scope.has_pending, "未知进程必须由任务保留 exact transport")

    async def test_event_driven_token_cancel_and_resource_reclamation_are_distinct(self):
        from tricoder.core.cancellation import CancellationToken, CancellationError
        from tricoder.mcp.client import MCPClient
        from tricoder.mcp.security import MCPLaunchRequest
        for phase in ("initialize", "request"):
            with self.subTest(phase=phase):
                harness = _EventDrivenSDKHarness(ControlledProcess(exit_on="terminate"))
                entered = asyncio.Event()

                async def blocked(*_args, **_kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                if phase == "initialize":
                    harness.session.initialize = blocked
                else:
                    harness.session.call_tool = blocked
                request = MCPLaunchRequest(command="python", args=(), cwd=".", env={}, approval_detail="synthetic")
                client = MCPClient("docs", request, sdk_loader=harness.sdk_loader,
                                   transport_factory=harness.transport_factory)
                token = CancellationToken()
                if phase == "request":
                    await client.start(token)
                task = asyncio.create_task(client.start(token) if phase == "initialize"
                                           else client.call_tool("echo", {}, token))
                await asyncio.wait_for(entered.wait(), 1)
                # 取消发生时进程必须仍然存活；自然退出等待设为 0 后，关闭路径
                # 会立即进入受控 terminate，并以 returncode 证明实际回收。
                self.assertIsNone(harness.process_harness.process.returncode)
                self.assertFalse(harness.process_harness.process.stdin.closed)
                with patch("tricoder.mcp.transport._NATURAL_EXIT_SECONDS", 0):
                    token.cancel()
                    with self.assertRaises(CancellationError):
                        await asyncio.wait_for(task, 1)
                    await client.stop()
                await asyncio.wait_for(harness.process_harness.termination_started.wait(), 1)
                transport = harness.transports[0]
                self.assertEqual(-15, harness.process_harness.process.returncode)
                self.assertEqual(["terminate"], harness.process_harness.process.signals)
                self.assertEqual(MCPProcessExitEvidence.VERIFIED, transport.outcome.process_exit)
                self.assertTrue(transport.outcome.resources_closed)
                self.assertTrue(harness.process_harness.process.stdin.closed)
                self.assertTrue(harness.process_harness.process.stdout.closed)

    def setUp(self):
        clock = ControlledClock()
        self.enterContext(patch("tricoder.mcp.transport.monotonic", clock.monotonic))
        self.enterContext(patch("tricoder.mcp.transport.sleep", clock.sleep))
        self.enterContext(patch("tricoder.mcp.transport._TERMINATE_SECONDS", 0.03))
        self.tasks = []
        create_task = asyncio.create_task

        def tracked_task(coroutine, **kwargs):
            task = create_task(coroutine, **kwargs)
            self.tasks.append(task)
            return task

        self.enterContext(patch("tricoder.mcp.transport.asyncio.create_task", tracked_task))

    def make_transport(self, process):
        harness = ProcessHarness(process)
        parameters = SimpleNamespace(command="approved", args=[], env={}, cwd=".")
        transport = VerifiedStdioTransport(
            parameters, errlog=io.StringIO(), bindings=harness.bindings()
        )
        return transport, harness

    def assert_closed(self, transport, harness):
        self.assertTrue(transport.outcome.resources_closed)
        self.assertTrue(harness.process.stdin.closed)
        self.assertTrue(harness.process.stdout.closed)
        self.assertTrue(harness.process.job_closed)
        self.assertTrue(harness.process.transport_closed)
        self.assertEqual(4, len(harness.endpoints))
        self.assertTrue(all(endpoint.closed for endpoint in harness.endpoints))

    async def test_natural_nonzero_exit_is_verified(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)
        self.assertEqual(MCPTransportOutcome(MCPProcessExitEvidence.NOT_STARTED, False), transport.outcome)
        async with transport:
            self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
            process.returncode = 7
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, transport.outcome.process_exit)
        self.assertEqual([], process.signals)
        self.assert_closed(transport, harness)
        with self.assertRaises(FrozenInstanceError):
            transport.outcome.resources_closed = False

    async def test_terminate_then_observed_exit_is_verified(self):
        process = ControlledProcess(exit_on="terminate")
        transport, harness = self.make_transport(process)
        async with transport:
            pass
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, transport.outcome.process_exit)
        self.assertEqual(-15, process.returncode)
        self.assertEqual(["terminate"], process.signals)
        self.assert_closed(transport, harness)

    async def test_kill_then_observed_exit_is_verified(self):
        process = ControlledProcess(exit_on="kill")
        transport, harness = self.make_transport(process)
        async with transport:
            pass
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, transport.outcome.process_exit)
        self.assertEqual(-9, process.returncode)
        self.assertEqual(["terminate", "kill"], process.signals)
        self.assert_closed(transport, harness)

    async def test_kill_without_observed_exit_is_unknown(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)
        async with transport:
            pass
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assertTrue(transport.outcome.resources_closed)
        self.assertIsNone(process.returncode)
        self.assertEqual(["terminate", "kill"], process.signals)
        self.assert_closed(transport, harness)

    async def test_eof_and_closed_pipes_do_not_prove_process_exit(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)
        async with transport:
            await process.stdout.aclose()
            await process.stdin.aclose()
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assertIsNone(process.returncode)
        self.assert_closed(transport, harness)

    async def test_unknown_exit_still_closes_every_owned_resource(self):
        transport, harness = self.make_transport(ControlledProcess())
        outsider = asyncio.create_task(asyncio.Event().wait())
        try:
            async with transport:
                pass
            self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
            self.assert_closed(transport, harness)
            self.assertFalse(outsider.done())
            self.assertTrue(all(task.done() for task in self.tasks if task is not outsider))
        finally:
            outsider.cancel()
            await asyncio.gather(outsider, return_exceptions=True)

    async def test_concurrent_transports_keep_outcomes_isolated(self):
        first, first_harness = self.make_transport(ControlledProcess())
        second, second_harness = self.make_transport(ControlledProcess())

        async def run(transport, process, code):
            async with transport:
                process.returncode = code
                await asyncio.sleep(0)

        await asyncio.gather(
            run(first, first_harness.process, 4),
            run(second, second_harness.process, None),
        )
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, first.outcome.process_exit)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, second.outcome.process_exit)
        self.assert_closed(first, first_harness)
        self.assert_closed(second, second_harness)

    async def test_native_cancellation_keeps_cleanup_bounded_and_unknown(self):
        transport, harness = self.make_transport(ControlledProcess())
        harness.hold_termination = True

        async def run():
            async with transport:
                pass

        owner = asyncio.create_task(run())
        await harness.termination_started.wait()
        started = asyncio.get_running_loop().time()
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 1.5)
        self.assertLess(asyncio.get_running_loop().time() - started, 1.5)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assertIsNone(harness.process.returncode)
        self.assert_closed(transport, harness)
        self.assertTrue(all(task.done() for task in self.tasks))

    async def test_reader_retains_suffix_and_bounds_invalid_line_exception(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)
        async with transport as (reader, writer):
            await process.stdout.send(b'{"jsonrpc":"2.0","id":1,"result":"')
            await process.stdout.send('中"}\n{invalid-sensitive-line}\n'.encode())
            first = await asyncio.wait_for(reader.receive(), 1)
            invalid = await asyncio.wait_for(reader.receive(), 1)
            self.assertEqual("中", first.message["result"])
            self.assertIsInstance(invalid, Exception)
            self.assertNotIn("sensitive", str(invalid))
            self.assertLessEqual(len(str(invalid)), 128)
            process.returncode = 0
        self.assert_closed(transport, harness)

    async def test_writer_emits_one_utf8_line_and_flushes_before_close(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)

        class Message:
            def model_dump_json(self, *, by_alias, exclude_unset):
                if not (by_alias and exclude_unset):
                    raise AssertionError("序列化必须使用协议别名并忽略未设置字段")
                return '{"jsonrpc":"2.0","method":"中文"}'

        async with transport as (reader, writer):
            await writer.send(SimpleNamespace(message=Message()))
            process.returncode = 0
        self.assertEqual(['{"jsonrpc":"2.0","method":"中文"}\n'.encode()], process.stdin.sent)
        self.assert_closed(transport, harness)

    async def test_failed_pipe_close_keeps_outcome_unknown_and_closes_other_resources(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)

        async def failed_close():
            raise OSError("close-failure-sentinel")

        process.stdout.aclose = failed_close
        async with transport:
            process.returncode = 0
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assertFalse(transport.outcome.resources_closed)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.job_closed)
        self.assertTrue(process.transport_closed)
        self.assertTrue(all(endpoint.closed for endpoint in harness.endpoints))
        self.assertTrue(all(task.done() for task in self.tasks))

    async def test_broken_resource_during_close_does_not_prove_resources_closed(self):
        from tricoder.mcp.sdk import load_mcp_sdk

        sdk = await asyncio.to_thread(load_mcp_sdk)
        process = ControlledProcess()
        harness = ProcessHarness(process)
        bindings = replace(
            harness.bindings(),
            closed_resource_errors=(*sdk.stdio_bindings.closed_resource_errors, ClosedStream),
            end_of_stream_errors=(*sdk.stdio_bindings.end_of_stream_errors, ClosedStream),
        )
        transport = VerifiedStdioTransport(object(), errlog=io.StringIO(), bindings=bindings)

        from anyio import BrokenResourceError

        async def failed_close():
            raise BrokenResourceError()

        async with transport as (reader, writer):
            reader.aclose = failed_close
            process.returncode = 0
        self.assertFalse(reader.closed)
        self.assertEqual(0, process.returncode)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.job_closed)
        self.assertTrue(process.transport_closed)
        self.assertTrue(all(endpoint.closed for endpoint in harness.endpoints if endpoint is not reader))
        self.assertTrue(all(task.done() for task in self.tasks))
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assertFalse(transport.outcome.resources_closed)

    async def test_process_creation_failure_closes_streams_without_claiming_a_process(self):
        harness = ProcessHarness(ControlledProcess())

        async def failed_create(parameters, errlog):
            raise OSError("spawn-failure")

        transport = VerifiedStdioTransport(
            object(), errlog=io.StringIO(),
            bindings=replace(harness.bindings(), create_process=failed_create),
        )
        with self.assertRaises(OSError):
            await transport.__aenter__()
        self.assertEqual(MCPProcessExitEvidence.NOT_STARTED, transport.outcome.process_exit)
        self.assertTrue(transport.outcome.resources_closed)
        self.assertTrue(all(endpoint.closed for endpoint in harness.endpoints))
        self.assertFalse(harness.process.job_closed)

    async def test_missing_pipe_after_spawn_never_reverts_to_not_started(self):
        process = ControlledProcess(exit_on="terminate")
        process.stdout = None
        transport, harness = self.make_transport(process)
        with self.assertRaisesRegex(RuntimeError, "mcp_stdio_missing_pipe"):
            await transport.__aenter__()
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, transport.outcome.process_exit)
        self.assertTrue(transport.outcome.resources_closed)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.job_closed)
        self.assertTrue(process.transport_closed)

    async def test_termination_exception_preserves_unknown_even_if_returncode_changes(self):
        process = ControlledProcess()
        harness = ProcessHarness(process)

        async def failed_terminate(child):
            child.returncode = 1
            raise RuntimeError("termination-failure")

        transport = VerifiedStdioTransport(
            object(), errlog=io.StringIO(),
            bindings=replace(harness.bindings(), terminate_process_tree=failed_terminate),
        )
        async with transport:
            pass
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assert_closed(transport, harness)

    async def test_body_cancellation_and_repeated_cancel_finish_owned_cleanup(self):
        process = ControlledProcess()
        transport, harness = self.make_transport(process)
        harness.hold_termination = True
        entered = asyncio.Event()

        async def run():
            async with transport:
                entered.set()
                await asyncio.Event().wait()

        owner = asyncio.create_task(run())
        await entered.wait()
        owner.cancel()
        await harness.termination_started.wait()
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 1.5)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assert_closed(transport, harness)

    async def test_real_sdk_memory_streams_transfer_protocol_messages_without_a_server(self):
        from tricoder.mcp.sdk import load_mcp_sdk

        sdk = await asyncio.to_thread(load_mcp_sdk)
        process = ControlledProcess()
        harness = ProcessHarness(process)
        bindings = replace(
            harness.bindings(),
            create_memory_object_stream=sdk.stdio_bindings.create_memory_object_stream,
            parse_message=sdk.stdio_bindings.parse_message,
            closed_resource_errors=(*sdk.stdio_bindings.closed_resource_errors, ClosedStream),
            end_of_stream_errors=(*sdk.stdio_bindings.end_of_stream_errors, ClosedStream),
        )
        transport = VerifiedStdioTransport(object(), errlog=io.StringIO(), bindings=bindings)
        async with transport as (reader, writer):
            await process.stdout.send(b'{"jsonrpc":"2.0","id":3,"result":{}}\n')
            incoming = await asyncio.wait_for(reader.receive(), 1)
            self.assertEqual(3, incoming.message.id)
            await writer.send(incoming)
            await asyncio.wait_for(process.stdin.received.wait(), 1)
            process.returncode = 0
        self.assertEqual([b'{"jsonrpc":"2.0","id":3,"result":{}}\n'], process.stdin.sent)
        self.assertEqual(MCPProcessExitEvidence.VERIFIED, transport.outcome.process_exit)
        self.assertTrue(transport.outcome.resources_closed)
        self.assertTrue(all(task.done() for task in self.tasks))
