"""最终审查反例：真实边界、内存资源及固定线程调度，不启动外部进程。"""

import asyncio
import dis
import importlib
import io
import logging
import os
import sys
import threading
import time
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.mcp import client as client_module
from tricoder.mcp import sdk as sdk_module
from tricoder.mcp.client import MCPClient, MCPCleanupError
from tricoder.mcp.manager import MCPManager
from tricoder.mcp.models import MCPServerState
from tricoder.mcp.runtime import run_mcp_task
from tricoder.mcp.sdk import MCPDependencyError, SDKLogSource, isolate_sdk_logs, load_mcp_sdk
from tricoder.mcp.security import MCPLaunchRequest
from tricoder.mcp.transport import MCPProcessExitEvidence, VerifiedStdioTransport
from tests import test_mcp_client as client_tests
from tests import test_mcp_runtime as runtime_tests
from tests import test_mcp_transport as transport_tests


class FinalTransportTests(unittest.IsolatedAsyncioTestCase):
    setUp = transport_tests.VerifiedStdioTransportTests.setUp
    make_transport = transport_tests.VerifiedStdioTransportTests.make_transport

    async def test_real_transport_close_binding_exposes_permission_failure(self):
        bindings = (await asyncio.to_thread(load_mcp_sdk)).stdio_bindings
        child = transport_tests.ControlledProcess()
        child.returncode = 0
        low = SimpleNamespace(closed=False)

        def denied_close():
            raise PermissionError("synthetic-close")

        low.close = denied_close
        low.is_closing = lambda: low.closed
        child._process = SimpleNamespace(_transport=low)
        harness = transport_tests.ProcessHarness(child)
        transport = VerifiedStdioTransport(object(), errlog=io.StringIO(), bindings=replace(
            harness.bindings(), close_subprocess_transport=bindings.close_subprocess_transport,
        ))
        async with transport:
            pass
        self.assertFalse(low.closed)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
        self.assertFalse(transport.outcome.resources_closed)

    async def test_real_transport_close_binding_requires_is_closing_evidence(self):
        bindings = (await asyncio.to_thread(load_mcp_sdk)).stdio_bindings
        child = transport_tests.ControlledProcess()
        child.returncode = 0
        child._process = SimpleNamespace(_transport=SimpleNamespace(
            close=lambda: None, is_closing=lambda: False,
        ))
        harness = transport_tests.ProcessHarness(child)
        transport = VerifiedStdioTransport(object(), errlog=io.StringIO(), bindings=replace(
            harness.bindings(), close_subprocess_transport=bindings.close_subprocess_transport,
        ))
        async with transport:
            pass
        self.assertFalse(transport.outcome.resources_closed)

    async def test_real_job_binding_retains_exact_failed_handle_until_success(self):
        windows = importlib.import_module("mcp.os.win32.utilities")
        child = transport_tests.ControlledProcess()
        child.returncode = 0
        job = SimpleNamespace(closed=False, terminated=False)
        jobs = weakref.WeakKeyDictionary()
        deny = True

        class SyntheticWin32Error(Exception):
            pass

        async def create(command, args, *, env, errlog, cwd):
            jobs[child] = job
            return child

        def close(handle):
            self.assertIs(job, handle)
            if deny:
                raise SyntheticWin32Error("synthetic-job-close")
            handle.closed = True

        def terminate(handle, code):
            self.assertIs(job, handle)
            handle.terminated = True

        with patch.object(windows, "_process_jobs", jobs), patch.object(
            windows, "create_windows_process", create,
        ), patch.object(windows, "win32api", SimpleNamespace(CloseHandle=close)), patch.object(
            windows, "win32job", SimpleNamespace(TerminateJobObject=terminate),
        ), patch.object(windows, "pywintypes", SimpleNamespace(error=SyntheticWin32Error)), patch.object(
            windows, "sys", SimpleNamespace(platform="win32"),
        ), patch.object(sdk_module, "sys", SimpleNamespace(platform="win32")):
            bindings = load_mcp_sdk().stdio_bindings
            harness = transport_tests.ProcessHarness(child)
            transport = VerifiedStdioTransport(
                SimpleNamespace(command="synthetic", args=[], env={}, cwd="."),
                errlog=io.StringIO(), bindings=replace(
                    harness.bindings(), create_process=bindings.create_process,
                    close_process_job=bindings.close_process_job,
                ),
            )
            async with transport:
                pass
            self.assertFalse(job.closed)
            self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)
            self.assertFalse(transport.outcome.resources_closed)
            # 失败后仍可使用 exact Job；不是“调用过 close”或 warning 的伪证据。
            await bindings.terminate_process_tree(child)
            self.assertTrue(job.terminated)
            self.assertFalse(job.closed)
            deny = False
            self.assertIs(True, bindings.close_process_job(child))
            self.assertTrue(job.closed)
            self.assertNotIn(child, jobs)

    async def test_transient_flush_close_failure_stays_unknown_after_final_success(self):
        child = transport_tests.ControlledProcess()
        transport, harness = self.make_transport(child)
        close = child.stdin.aclose
        first = True

        async def transient():
            nonlocal first
            if first:
                first = False
                raise RuntimeError("synthetic-transient-close")
            await close()

        child.stdin.aclose = transient
        async with transport:
            child.returncode = 0
        self.assertTrue(child.stdin.closed)
        self.assertTrue(transport.outcome.resources_closed)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)

    async def check_bridge_fault(self, mode):
        child = transport_tests.ControlledProcess()
        transport, harness = self.make_transport(child)
        reached = asyncio.Event()

        async def bad_read():
            reached.set()
            raise RuntimeError("synthetic-read")

        class Message:
            def model_dump_json(self, **kwargs):
                if mode == "serialize":
                    reached.set()
                    raise ValueError("synthetic-serialize")
                return "{}"

        async def bad_write(data):
            reached.set()
            if mode == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic-write")

        if mode == "reader":
            child.stdout.receive = bad_read
        else:
            child.stdin.send = bad_write
        with patch("tricoder.mcp.transport._WRITER_FLUSH_SECONDS", 0.02):
            async with transport as (_, writer):
                if mode != "reader":
                    await writer.send(SimpleNamespace(message=Message()))
                await reached.wait()
                await asyncio.sleep(0)
                child.returncode = 0
        self.assertTrue(transport.outcome.resources_closed)
        self.assertEqual(MCPProcessExitEvidence.UNKNOWN, transport.outcome.process_exit)

    async def test_reader_failure_is_sticky(self):
        await self.check_bridge_fault("reader")

    async def test_writer_failure_is_sticky(self):
        await self.check_bridge_fault("writer")

    async def test_writer_serialization_failure_is_sticky(self):
        await self.check_bridge_fault("serialize")

    async def test_writer_flush_timeout_is_sticky(self):
        await self.check_bridge_fault("timeout")

    async def check_primary(self, primary, *, enter=False):
        child = transport_tests.ControlledProcess()
        transport, harness = self.make_transport(child)
        harness.hold_termination = True
        reached = asyncio.Event()
        observed = []

        if enter:
            child.stdout = None

        async def run():
            if enter:
                # Observe the actual exception produced by partial enter; no replacement.
                original_exit = transport.__aexit__

                async def observe_exit(*args):
                    observed.append(sys.exception())
                    return await original_exit(*args)

                transport.__aexit__ = observe_exit
            async with transport:
                reached.set()
                try:
                    if primary is None:
                        await asyncio.Event().wait()
                    else:
                        raise primary
                except BaseException as first:
                    observed.append(first)
                    raise

        owner = asyncio.create_task(run())
        if not enter:
            await reached.wait()
            if primary is None:
                owner.cancel("first")
        await harness.termination_started.wait()
        started = asyncio.get_running_loop().time()
        owner.cancel("second")
        caught = None
        try:
            await owner
        except BaseException as exc:
            caught = exc
        self.assertLess(asyncio.get_running_loop().time() - started, 0.3)
        self.assertIs(observed[0], caught)
        self.assertTrue(transport.outcome.resources_closed)

    async def test_body_native_primary_survives_second_cancel(self):
        await self.check_primary(None)

    async def test_body_token_primary_survives_native_cancel(self):
        await self.check_primary(CancellationError("first-token"))

    async def test_body_business_primary_survives_native_cancel(self):
        await self.check_primary(ValueError("first-business"))

    async def test_partial_enter_primary_survives_native_cancel(self):
        await self.check_primary(None, enter=True)

    async def test_pre_spawn_native_and_token_primary_survive_cleanup_cancel(self):
        for primary in (None, CancellationError("first-token")):
            with self.subTest(token=primary is not None):
                harness = transport_tests.ProcessHarness(transport_tests.ControlledProcess())
                reached, closing = asyncio.Event(), asyncio.Event()
                observed = []
                streams = harness.streams

                def delayed_streams(capacity):
                    pair = streams(capacity)
                    original = pair[0].aclose

                    async def close():
                        closing.set()
                        await asyncio.sleep(0.03)
                        await original()

                    pair[0].aclose = close
                    return pair

                async def create(parameters, errlog):
                    reached.set()
                    try:
                        if primary is not None:
                            raise primary
                        await asyncio.Event().wait()
                    except BaseException as first:
                        observed.append(first)
                        raise

                transport = VerifiedStdioTransport(object(), errlog=io.StringIO(), bindings=replace(
                    harness.bindings(), create_process=create, create_memory_object_stream=delayed_streams,
                ))
                task = asyncio.create_task(transport.__aenter__())
                await reached.wait()
                if primary is None:
                    task.cancel("first")
                await closing.wait()
                started = time.perf_counter()
                task.cancel("second")
                try:
                    await task
                except BaseException as error:
                    self.assertIs(observed[0], error)
                else:
                    self.fail("必须保留主异常")
                self.assertLess(time.perf_counter() - started, 0.3)
                self.assertTrue(transport.outcome.resources_closed)


class FinalClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_timeout_primary_survives_abort_cancel(self):
        harness = client_tests._ControlledSDKHarness(transport_tests.ControlledProcess(exit_on="terminate"))
        harness.session.initialize_gate = asyncio.Event()
        harness.session_exit_delay = 0.06
        request = MCPLaunchRequest(command="synthetic", args=(), cwd=Path.cwd(), env={}, approval_detail="approved")
        client = MCPClient("docs", request, initialize_timeout=0.06, cleanup_timeout=0.3,
                           sdk_loader=harness.sdk_loader, transport_factory=harness.transport_factory)
        observed = []
        original = client_module._wait_for_start_ready
        clock = transport_tests.ControlledClock()

        async def observe(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except BaseException as error:
                observed.append(error)
                raise

        with patch.object(client_module, "_wait_for_start_ready", observe), patch(
            "tricoder.mcp.transport.monotonic", clock.monotonic,
        ), patch("tricoder.mcp.transport.sleep", clock.sleep):
            task = asyncio.create_task(client.start(CancellationToken()))
            while "session.exit" not in harness.events:
                await asyncio.sleep(0)
            owner = client._lifecycle_task
            started = time.perf_counter()
            task.cancel("second")
            try:
                await task
            except BaseException as error:
                caught = error
            await asyncio.gather(owner, return_exceptions=True)
        self.assertLess(time.perf_counter() - started, 0.9)
        self.assertIs(observed[0], caught)
        self.assertIsInstance(caught, client_module.MCPTimeoutError)

    async def check_start_cancellation(self, *, token_primary):
        harness = client_tests._ControlledSDKHarness(transport_tests.ControlledProcess(exit_on="terminate"))
        harness.session.initialize_gate = asyncio.Event()
        harness.session_exit_delay = 0.1
        request = MCPLaunchRequest(command="synthetic", args=(), cwd=Path.cwd(), env={}, approval_detail="approved")
        client = MCPClient("docs", request, initialize_timeout=1, cleanup_timeout=0.25,
                           sdk_loader=harness.sdk_loader, transport_factory=harness.transport_factory)
        clock = transport_tests.ControlledClock()
        token = CancellationToken()
        original = client_module._wait_for_start_ready
        observed = []

        async def observe(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except BaseException as first:
                observed.append(first)
                raise

        with patch.object(client_module, "_wait_for_start_ready", observe), patch(
            "tricoder.mcp.transport.monotonic", clock.monotonic,
        ), patch("tricoder.mcp.transport.sleep", clock.sleep):
            owner = asyncio.create_task(client.start(token))
            while "initialize" not in harness.events:
                await asyncio.sleep(0)
            lifecycle = client._lifecycle_task
            started = time.perf_counter()
            if token_primary:
                token.cancel()
            else:
                owner.cancel("first")
            while "session.exit" not in harness.events:
                await asyncio.sleep(0)
            owner.cancel("second")
            caught = None
            try:
                await owner
            except BaseException as exc:
                caught = exc
            elapsed = time.perf_counter() - started
            await asyncio.gather(lifecycle, return_exceptions=True)
        self.assertLess(elapsed, 0.95)  # 0.25 cleanup + 0.5 reap + 调度余量，不随第二次取消重置。
        self.assertIs(observed[0], caught)
        self.assertEqual(MCPServerState.FAILED, client.state)

    async def test_start_native_primary_survives_abort_reap_cancel(self):
        await self.check_start_cancellation(token_primary=False)

    async def test_start_token_primary_survives_abort_reap_cancel(self):
        await self.check_start_cancellation(token_primary=True)

    async def test_start_ready_reap_preserves_first_native_exception(self):
        ready = asyncio.get_running_loop().create_future()
        reaping = asyncio.Event()
        observed = []
        original_wait = asyncio.wait

        async def token_wait(token):
            try:
                await asyncio.Event().wait()
            finally:
                reaping.set()
                await asyncio.sleep(0.05)

        async def observe_wait(*args, **kwargs):
            try:
                return await original_wait(*args, **kwargs)
            except asyncio.CancelledError as first:
                observed.append(first)
                raise

        with patch.object(client_module, "_wait_for_cancellation", token_wait), patch.object(
            asyncio, "wait", observe_wait,
        ):
            task = asyncio.create_task(client_module._wait_for_start_ready(ready, timeout=1, cancellation=CancellationToken()))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel("first")
            await reaping.wait()
            task.cancel("second")
            try:
                await task
            except BaseException as error:
                self.assertIs(observed[0], error)
            else:
                self.fail("必须保留主异常")


class FinalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = runtime_tests.MCPRuntimeTests.setUp
    tearDown = runtime_tests.MCPRuntimeTests.tearDown

    async def check_pre_spawn(self, primary=None):
        harness = client_tests._ControlledSDKHarness()
        original_streams = harness.process_harness.streams

        async def fail_close():
            raise RuntimeError("synthetic-stream-close")

        def streams(capacity):
            pair = original_streams(capacity)
            pair[0].aclose = fail_close
            return pair

        async def fail_spawn(parameters, errlog):
            raise RuntimeError("synthetic-spawn")

        harness.process_harness.streams = streams
        harness.process_harness.create = fail_spawn
        self.registry.context.approver = lambda *_: True
        clients, managers, events = [], [], []

        def make_client(server_id, request):
            client = MCPClient(server_id, request, sdk_loader=harness.sdk_loader, transport_factory=harness.transport_factory)
            clients.append(client)
            return client

        def factory(config, context, source_env, audit):
            manager = MCPManager(config, context, source_env, audit, client_factory=make_client)
            managers.append(manager)
            return manager

        async def operation(_registry):
            if primary is not None:
                raise primary
            return "must-not-succeed"

        with self.assertRaises(type(primary) if primary is not None else MCPCleanupError) as caught:
            await run_mcp_task(self.config, self.registry, source_env={}, audit=SimpleNamespace(log=events.append),
                               cancellation=self.token, operation=operation, manager_factory=factory)
        if primary is not None:
            self.assertIs(primary, caught.exception)
        self.assertEqual(MCPProcessExitEvidence.NOT_STARTED, harness.transports[0].outcome.process_exit)
        self.assertFalse(harness.transports[0].outcome.resources_closed)
        self.assertTrue(managers[0]._host._stop_pending)
        for _ in range(2):
            with self.assertRaises(MCPCleanupError):
                await clients[0].stop()
            with self.assertRaises(MCPCleanupError):
                await managers[0].stop_all()
        self.assertTrue(any(event.get("phase") == "task_cleanup" for event in events))

    async def test_spawn_failure_and_unclosed_stream_prevent_runtime_success(self):
        await self.check_pre_spawn()

    async def test_spawn_failure_cleanup_keeps_native_primary(self):
        await self.check_pre_spawn(asyncio.CancelledError("first-native"))

    async def test_spawn_failure_cleanup_keeps_token_primary(self):
        await self.check_pre_spawn(CancellationError("first-token"))


class FinalLoggingTests(unittest.TestCase):
    def check_host_update(self, *, install, remove=False):
        logger = logging.getLogger("mcp.client.stdio")
        original = logger.filters
        host = logging.Filter()
        if remove:
            logger.addFilter(host)
        source = SDKLogSource(logger.name, os.path.abspath("synthetic-sdk.py"))
        reached, resume = threading.Event(), threading.Event()
        errors = []
        code = isolate_sdk_logs.__wrapped__.__code__
        offsets = {instruction.offset for instruction in dis.get_instructions(code)
                   if instruction.opname == "STORE_ATTR" and instruction.argval == "filters"}
        stores = 0

        def trace(frame, event, arg):
            nonlocal stores
            if frame.f_code is code:
                frame.f_trace_opcodes = True
                if event == "opcode" and frame.f_lasti in offsets:
                    stores += 1
                    if stores == (1 if install else 2):
                        reached.set()
                        if not resume.wait(3):
                            raise AssertionError("synthetic scheduler timeout")
            return trace

        def worker():
            try:
                sys.settrace(trace)
                with isolate_sdk_logs((source,)):
                    pass
            except BaseException as exc:
                errors.append(exc)
            finally:
                sys.settrace(None)

        thread = threading.Thread(target=worker)
        try:
            thread.start()
            self.assertTrue(reached.wait(3))
            if remove:
                logger.removeFilter(host)
            else:
                logger.addFilter(host)
            resume.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(not remove, host in logger.filters)
            self.assertIs(original, logger.filters)
        finally:
            resume.set()
            thread.join(3)
            logger.removeFilter(host)

    def test_install_preserves_concurrent_standard_add_filter(self):
        self.check_host_update(install=True)

    def test_release_preserves_concurrent_standard_add_filter(self):
        self.check_host_update(install=False)

    def test_install_preserves_concurrent_standard_remove_filter(self):
        self.check_host_update(install=True, remove=True)

    def test_release_preserves_concurrent_standard_remove_filter(self):
        self.check_host_update(install=False, remove=True)

    def test_wholesale_host_replacement_is_not_overwritten(self):
        logger = logging.getLogger("mcp.client.stdio")
        original = logger.filters
        replacement = [logging.Filter()]
        try:
            with isolate_sdk_logs((SDKLogSource(logger.name, os.path.abspath("synthetic-sdk.py")),)):
                logger.filters = replacement
            self.assertIs(replacement, logger.filters)
        finally:
            logger.filters = original


class FinalShapeTests(unittest.TestCase):
    def test_required_async_helper_shape_rejected_before_spawn(self):
        import anyio
        windows = importlib.import_module("mcp.os.win32.utilities")
        posix = importlib.import_module("mcp.os.posix.utilities")

        async def wrong_arguments():
            raise AssertionError("must not execute")

        def not_async(*args, **kwargs):
            raise AssertionError("must not execute")

        for module, name in ((anyio, "open_process"), (windows, "create_windows_process"),
                             (posix, "terminate_posix_process_tree")):
            for invalid in (wrong_arguments, not_async):
                with self.subTest(helper=name, invalid=invalid.__name__), patch.object(module, name, invalid):
                    with self.assertRaises(MCPDependencyError):
                        load_mcp_sdk()

    def test_required_sync_helper_shape_rejected_before_spawn(self):
        import anyio

        async def wrong_async(*args, **kwargs):
            raise AssertionError("must not execute")

        def wrong_arguments():
            raise AssertionError("must not execute")

        class AsyncCallable:
            async def __call__(self, capacity):
                raise AssertionError("must not execute")

        for invalid in (wrong_async, wrong_arguments, AsyncCallable()):
            with self.subTest(invalid=type(invalid).__name__), patch.object(anyio, "create_memory_object_stream", invalid):
                with self.assertRaises(MCPDependencyError):
                    load_mcp_sdk()

    def test_windows_mapping_and_native_api_shape_rejected_before_spawn(self):
        windows = importlib.import_module("mcp.os.win32.utilities")
        for name, invalid in (("_process_jobs", None), ("_process_jobs", {}),
                              ("win32api", SimpleNamespace(CloseHandle=lambda: None)),
                              ("win32job", SimpleNamespace(TerminateJobObject=lambda: None))):
            with self.subTest(name=name), patch.object(windows, name, invalid), patch.object(
                sdk_module, "sys", SimpleNamespace(platform="win32"),
            ):
                with self.assertRaises(MCPDependencyError):
                    load_mcp_sdk()
