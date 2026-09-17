import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.core.cancellation import CancellationToken
from tricoder.core.events import (
    ApprovalRequested,
    ProviderCompleted,
    RoundStarted,
    RuntimeCompleted,
    RuntimeFailed,
    TextDelta,
    ToolCallCompleted,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    UsageReported,
)
from tricoder.models import (
    Message,
    ProviderResponse,
    SessionContext,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class HybridProvider:
    """同步和流式入口表达同一组确定性响应。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        return self.responses.pop(0)

    async def stream(self, messages, tools=(), *, cancellation=None):  # type: ignore[no-untyped-def]
        response = self.responses.pop(0)
        if response.content:
            yield TextDelta(response.content)
        for call in response.tool_calls:
            yield ToolCallCompleted(call)
        if response.usage is not None:
            yield UsageReported(response.usage)
        yield ProviderCompleted(response.finish_reason)


class AsyncRegistry:
    """保留 Agent 工具边界的轻量测试实现，不模拟网络或文件系统。"""

    definitions: tuple[ToolDefinition, ...] = ()

    def __init__(self, *, cancel_after_first: CancellationToken | None = None) -> None:
        self.calls: list[str] = []
        self.cancel_after_first = cancel_after_first

    def contains(self, name: str) -> bool:
        return name in {"inspect", "finish"}

    def describe(self, name: str) -> ToolDefinition | None:
        if not self.contains(name):
            return None
        return ToolDefinition(name, "测试工具", {"type": "object"})

    def requires_approval(self, name: str) -> bool:
        return name == "inspect"

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        self.calls.append(name)
        return ToolResult(True, str(arguments.get("summary", "inspected")))

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        result = self.execute(name, arguments)
        if self.cancel_after_first is not None and len(self.calls) == 1:
            self.cancel_after_first.cancel()
        return result


def _finish_response(summary: str = "done") -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(ToolCall("finish-1", "finish", {"summary": summary}),),
        finish_reason="tool_calls",
        usage=TokenUsage(8, 2, 3, None),
    )


class AsyncAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_queued_worker_does_not_start_tool_after_caller_left(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from tricoder.extensions.models import ToolOrigin
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.tools import ToolContext, ToolRegistry
        from tricoder.tools.handlers import ToolHandler
        with tempfile.TemporaryDirectory() as raw, ThreadPoolExecutor(max_workers=1) as executor:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(executor)
            release, started = threading.Event(), []
            blocker = loop.run_in_executor(None, release.wait, 3)
            class Tool(ToolHandler):
                name, description = "queued", "queued work must not start after cancel"
                parameters = ToolHandler._schema({})
                def run(inner, arguments):
                    started.append(True)
                    return ToolResult(True, "unexpected action")
            registry = ToolRegistry(ToolContext(WorkspacePolicy(Path(raw)), CommandPolicy(), lambda *_: True))
            registry.register(Tool(registry.context), origin=ToolOrigin("hook", "queued", "read"))
            task = asyncio.create_task(registry.execute_async("queued", {}))
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            try:
                await checkpoint.wait()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                release.set()
                await blocker
                await loop.run_in_executor(None, lambda: None)
            self.assertEqual([], started, "shield 不得让取消前尚未开始的工具产生迟到副作用")
            self.assertFalse(registry.has_pending_cleanup)

    async def test_distinct_worker_leases_survive_first_clean_completion(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from tricoder.extensions.models import ToolOrigin
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.task_cleanup import TaskCleanup, cleanup_scope
        from tricoder.tools import ToolContext, ToolRegistry
        from tricoder.tools.handlers import ToolHandler
        for workers in (1, 2):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as raw, \
                    ThreadPoolExecutor(max_workers=workers) as executor:
                loop = asyncio.get_running_loop()
                loop.set_default_executor(executor)
                entered = [asyncio.Event(), asyncio.Event()]
                release = [threading.Event(), threading.Event()]
                futures = []
                scope = TaskCleanup()
                class Worker(ToolHandler):
                    name, description = "worker", "concurrent worker"
                    parameters = ToolHandler._schema({"index": {"type": "integer"}}, ["index"])
                    def run(inner, arguments):
                        index = arguments["index"]
                        loop.call_soon_threadsafe(entered[index].set)
                        if not release[index].wait(3):
                            raise AssertionError("worker release missing")
                        return ToolResult(True, "clean")
                registry = ToolRegistry(ToolContext(WorkspacePolicy(Path(raw)), CommandPolicy(), lambda *_: True))
                registry.register(Worker(registry.context), origin=ToolOrigin("hook", "workers", "read"))
                original_submit = loop.run_in_executor
                def submit(*args):
                    future = original_submit(*args)
                    futures.append(future)
                    return future
                try:
                    with patch.object(loop, "run_in_executor", submit), cleanup_scope(scope):
                        first = asyncio.create_task(registry.execute_async("worker", {"index": 0}))
                        await asyncio.wait_for(entered[0].wait(), 1)
                        second = asyncio.create_task(registry.execute_async("worker", {"index": 1}))
                        checkpoint = asyncio.Event()
                        loop.call_soon(checkpoint.set)
                        await checkpoint.wait()
                        if workers == 2:
                            await asyncio.wait_for(entered[1].wait(), 1)
                        first.cancel()
                        await asyncio.gather(first, return_exceptions=True)
                        if workers == 2:
                            second.cancel()
                            await asyncio.gather(second, return_exceptions=True)
                        self.assertFalse(registry.execute("finish", {"summary": "same cancelled scope"}).ok,
                                         "同一 scope 在 caller 取消后也不得绕过在途阻断")
                    release[0].set()
                    await asyncio.wait_for(asyncio.shield(futures[0]), 1)
                    # 第二个 lease 已提交；第一个结束不能释放尚未完成的排队/并发工作。
                    await asyncio.wait_for(entered[1].wait(), 1)
                    if workers == 1:
                        second.cancel()
                        await asyncio.gather(second, return_exceptions=True)
                    self.assertTrue(registry.has_pending_cleanup)
                    self.assertTrue(scope.has_pending)
                    self.assertFalse(registry.execute("finish", {"summary": "still busy"}).ok)
                    self.assertFalse(registry._standalone_cleanup.retry(1))
                    release[1].set()
                    await asyncio.wait_for(asyncio.shield(futures[1]), 1)
                    self.assertFalse(registry.has_pending_cleanup)
                    self.assertFalse(scope.has_pending)
                    self.assertTrue(scope.failed)
                finally:
                    for gate in release:
                        gate.set()
                    await asyncio.gather(*futures, return_exceptions=True)

    async def test_executor_submission_failure_releases_unstarted_lease(self):
        from concurrent.futures import ThreadPoolExecutor
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.task_cleanup import current_cleanup
        from tricoder.tools import ToolContext, ToolRegistry
        with tempfile.TemporaryDirectory() as raw:
            executor = ThreadPoolExecutor(max_workers=1)
            asyncio.get_running_loop().set_default_executor(executor)
            executor.shutdown(wait=True)
            registry = ToolRegistry(ToolContext(WorkspacePolicy(Path(raw)), CommandPolicy(), lambda *_: True))
            with self.assertRaises(RuntimeError):
                await registry.execute_async("finish", {"summary": "executor closed"})
            self.assertFalse(registry.has_pending_cleanup)
            self.assertIsNone(current_cleanup())

    async def test_real_command_late_cleanup_remains_owned_after_native_caller_cancel(self):
        import gc
        import threading
        import time
        import weakref
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from tricoder import subprocess_control as control
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.task_cleanup import current_cleanup
        from tricoder.tools import ToolContext, ToolRegistry
        with tempfile.TemporaryDirectory() as raw, ThreadPoolExecutor(max_workers=1) as executor:
            root, loop = Path(raw), asyncio.get_running_loop()
            loop.set_default_executor(executor)
            (root / "owned.py").write_text("pass\n", encoding="utf-8")
            entered, release = asyncio.Event(), threading.Event()
            scopes = []
            def approve(*args):
                scopes.append(weakref.ref(current_cleanup()))
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("approval release missing")
                return True
            registry = ToolRegistry(ToolContext(WorkspacePolicy(root), CommandPolicy(root), approve))
            original = control._ProcessResources.cleanup
            def uncertain(resource, deadline):
                self.assertTrue(original(resource, deadline))
                return False
            with patch.object(control._ProcessResources, "cleanup", uncertain):
                task = asyncio.create_task(registry.execute_async("run_command", {"command": "python owned.py"}))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                finally:
                    release.set()
                    await loop.run_in_executor(None, lambda: None)
            del task
            gc.collect()
            self.assertIsNotNone(scopes[0](), "真实 command 的迟到清理丢失 scope owner")
            self.assertTrue(registry.has_pending_cleanup)
            self.assertFalse(registry.execute("finish", {"summary": "blocked"}).ok)
            self.assertTrue(registry._standalone_cleanup.retry(time.monotonic() + 1))

    async def test_cancelled_async_call_keeps_late_worker_resources_owned(self):
        await self.check_late_worker_owner(failed=True)

    async def test_clean_late_worker_releases_inflight_ownership(self):
        await self.check_late_worker_owner(failed=False)

    async def test_late_worker_exception_is_consumed_without_losing_resource_owner(self):
        import gc
        loop = asyncio.get_running_loop()
        previous, errors = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda _loop, event: errors.append(event))
        try:
            await self.check_late_worker_owner(failed=True, raise_after=True)
            gc.collect()
            self.assertEqual([], errors, "迟到 worker 异常没有被消费")
        finally:
            loop.set_exception_handler(previous)

    async def check_late_worker_owner(self, *, failed, raise_after=False):
        import gc
        import threading
        import time
        import weakref
        from concurrent.futures import ThreadPoolExecutor
        from tricoder.execution_state import ErrorCode
        from tricoder.extensions.models import ToolOrigin
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.task_cleanup import current_cleanup
        from tricoder.tools import ToolContext, ToolRegistry
        from tricoder.tools.handlers import ToolHandler

        for entry in ("registry", "agent", "agent_sync_registry"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as raw, \
                    ThreadPoolExecutor(max_workers=1) as executor:
                loop = asyncio.get_running_loop()
                loop.set_default_executor(executor)
                entered = asyncio.Event()
                release, reclaimed = threading.Event(), threading.Event()
                scopes, resources = [], []
                class Resource:
                    def cleanup(self, deadline):
                        return reclaimed.is_set()
                class LateTool(ToolHandler):
                    name, description = "late_worker", "event-controlled worker"
                    parameters = ToolHandler._schema({})
                    def run(inner, arguments):
                        scope = current_cleanup()
                        scopes.append(weakref.ref(scope))
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(3):
                            raise AssertionError("worker release missing")
                        if failed:
                            resource = Resource()
                            resources.append(weakref.ref(resource))
                            scope.mark_failed()
                            scope.retain(resource)
                        if raise_after:
                            raise ValueError("late worker error must be consumed")
                        return ToolResult(True, "worker completed")
                registry = ToolRegistry(ToolContext(WorkspacePolicy(Path(raw)), CommandPolicy(), lambda *_: True))
                registry.register(LateTool(registry.context), origin=ToolOrigin("hook", "late", "read"))
                if entry == "agent_sync_registry":
                    registry.execute_async = None
                agent = CodingAgent(HybridProvider([ProviderResponse(tool_calls=(
                    ToolCall("late", "late_worker", {}), ToolCall("finish", "finish", {"summary": "done"}),
                ))]), registry, plan_enabled=False)
                request = (registry.execute_async("late_worker", {}) if entry == "registry"
                           else agent.run_with_context_async("task", SessionContext()))
                task = asyncio.create_task(request)
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel("caller left before worker cleanup")
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    else:
                        self.fail("caller native cancellation did not return")
                    early = registry.execute("finish", {"summary": "must remain busy"})
                finally:
                    release.set()
                    # 单线程 executor 的后续哨兵证明真正的 worker wrapper 已执行完 finally。
                    await loop.run_in_executor(None, lambda: None)
                del task, request
                gc.collect()
                if failed:
                    self.assertIsNotNone(scopes[0](), "caller 已离开，迟到登记的 scope 被 GC")
                    scope = scopes[0]()
                    self.assertTrue(scope.failed)
                    self.assertIsNotNone(resources[0](), "exact late resource 没有持久 owner")
                    blocked = registry.execute("finish", {"summary": "must remain blocked"})
                    self.assertIs(blocked.error.code, ErrorCode.CLEANUP_FAILED)
                    self.assertFalse(registry._standalone_cleanup.retry(time.monotonic() + 1))
                    reclaimed.set()
                    self.assertTrue(registry._standalone_cleanup.retry(time.monotonic() + 1))
                    self.assertTrue(scope.failed)
                else:
                    self.assertFalse(registry.has_pending_cleanup, "clean late worker 留下永久 lease")
                    self.assertFalse(agent._cleanup_owner.has_pending)
                    self.assertIsNone(scopes[0](), "clean scope 在 worker 完成后仍有无用强引用")
                    self.assertTrue(registry.execute("finish", {"summary": "clean"}).ok)
                self.assertFalse(early.ok, "worker 仍在途时不得开始另一个操作")
                self.assertIs(early.error.code, ErrorCode.CLEANUP_FAILED)
                self.assertIsNone(current_cleanup())

    async def test_agent_scope_nesting_and_exception_handoff_preserve_exact_owner(self):
        import gc
        import weakref
        from contextlib import nullcontext
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.task_cleanup import TaskCleanup, cleanup_scope, current_cleanup
        from tricoder.tools import ToolContext, ToolRegistry

        for ambient in (False, True):
            with self.subTest(ambient=ambient), tempfile.TemporaryDirectory() as raw:
                registry = ToolRegistry(ToolContext(WorkspacePolicy(Path(raw)), CommandPolicy(), lambda *_: True))
                outer = TaskCleanup() if ambient else None
                refs = []
                primary = ValueError("synthetic boundary failure")
                class Resource:
                    def cleanup(self, deadline):
                        return True
                resource = Resource()
                def sink(event):
                    if isinstance(event, ToolExecutionStarted):
                        scope = current_cleanup()
                        refs.append(weakref.ref(scope))
                        if ambient:
                            self.assertIs(scope, outer)
                        scope.mark_failed()
                        scope.retain(resource)
                        raise primary
                agent = CodingAgent(HybridProvider([_finish_response()]), registry, plan_enabled=False)
                with cleanup_scope(outer) if ambient else nullcontext():
                    try:
                        await agent.run_with_context_async("task", SessionContext(), event_sink=sink)
                    except ValueError as error:
                        self.assertIs(error, primary)
                    else:
                        self.fail("首异常未传播")
                    self.assertIs(current_cleanup(), outer)
                primary = None  # 去掉测试保存的首异常引用，GC 不能依赖 traceback 充当 owner。
                gc.collect()
                self.assertIsNotNone(refs[0]())
                self.assertTrue(refs[0]().failed)
                self.assertTrue(refs[0]().has_pending)
                self.assertEqual(not ambient, registry.has_pending_cleanup)
                owner = outer if ambient else registry._standalone_cleanup
                self.assertTrue(owner.retry(1))
                self.assertTrue(refs[0]() is None or refs[0]().failed)
                self.assertIsNone(current_cleanup())

    async def test_standalone_agent_and_direct_registry_get_fresh_cleanup_deadlines(self):
        import io
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from tricoder import subprocess_control as control
        from tricoder.execution_state import ErrorCode
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.task_cleanup import current_cleanup
        from tricoder.tools import ToolContext, ToolRegistry
        from tests.test_mcp_transport import ControlledClock

        for entry, phase in ((entry, phase) for entry in ("agent", "registry_sync", "registry_async")
                             for phase in ("timeout", "output_limit")):
            with self.subTest(entry=entry, phase=phase), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "owned.py").write_text("pass\n", encoding="utf-8")
                clock, seen = ControlledClock(), []
                registry = ToolRegistry(ToolContext(WorkspacePolicy(root), CommandPolicy(root),
                                                    lambda *_: True, timeout=0))
                if phase == "output_limit":
                    registry.context.max_output_chars = 1
                first, second = Mock(returncode=None), Mock(returncode=0)
                for process in (first, second):
                    process.stdout, process.stderr = io.BytesIO(), io.BytesIO()
                    process.poll.side_effect = lambda process=process: process.returncode
                    process.wait.side_effect = lambda **_: 0
                if phase == "output_limit":
                    first.stdout = io.BytesIO(b"XX")
                def reader(*, target, args, daemon):
                    return SimpleNamespace(start=lambda: target(*args), is_alive=lambda: False)
                def terminate(process, env, job, *, deadline):
                    seen.append((current_cleanup(), deadline))
                    process.returncode = 0
                    return deadline > clock.now
                provider = HybridProvider([ProviderResponse(tool_calls=(
                    ToolCall(f"command-{index}", "run_command", {"command": "python owned.py"}),
                    ToolCall(f"finish-{index}", "finish", {"summary": "done"}),
                )) for index in (1, 2)])
                agent = CodingAgent(provider, registry, plan_enabled=False, max_rounds=1)
                async def execute(index):
                    if entry == "agent":
                        events = []
                        await agent.run_with_context_async(f"task-{index}", SessionContext(), event_sink=events.append)
                        return next(event.result for event in events if isinstance(event, ToolExecutionCompleted))
                    if entry == "registry_sync":
                        return registry.execute("run_command", {"command": "python owned.py"})
                    return await registry.execute_async("run_command", {"command": "python owned.py"})
                with patch.object(control, "time", SimpleNamespace(monotonic=clock.monotonic)), \
                     patch.object(control, "threading", SimpleNamespace(
                         Thread=reader, Lock=threading.Lock, Event=threading.Event)), \
                     patch("tricoder.task_cleanup.time", SimpleNamespace(monotonic=clock.monotonic)), \
                     patch.object(control.subprocess, "Popen", side_effect=(first, second)), \
                     patch.object(control, "_create_windows_job", return_value=Mock()), \
                     patch.object(control, "_terminate_process_tree", side_effect=terminate):
                    self.assertIsNone(current_cleanup())
                    result = await execute(1)
                    self.assertIs(result.error.code, ErrorCode.TIMEOUT if phase == "timeout" else ErrorCode.OUTPUT_LIMIT)
                    self.assertFalse(registry._standalone_cleanup.has_pending)
                    self.assertIsNone(current_cleanup(), "公开入口必须 reset ContextVar")
                    clock.now = 100.0
                    registry.context.timeout = 1
                    # T5 的 UNKNOWN 需显式确认；模拟 /clear 只换文件证据能力，
                    # 仍保留同一个 registry 和 T4 cleanup owner 来验证 deadline 隔离。
                    from tricoder.verification import VerificationScope
                    registry.context.verification_scope = VerificationScope()
                    result = await execute(2)
                    self.assertTrue(result.ok, "已完成旧任务的 deadline 污染了后续合法命令")
                    self.assertIsNone(current_cleanup())
                self.assertEqual([5.0, 105.0], [deadline for _, deadline in seen])
                self.assertIsNot(seen[0][0], seen[1][0])
                self.assertEqual(5.0, seen[0][0].started_deadline)
                self.assertIsNone(seen[1][0].started_deadline)
                self.assertIsNone(registry._standalone_cleanup.started_deadline,
                                  "持久 pending owner 不得用作任务执行 scope")

    async def test_already_cancelled_task_does_not_start_planning_provider(self):
        token = CancellationToken()
        token.cancel()
        started = []
        class Provider:
            async def stream(self, *args, **kwargs):
                started.append(True)
                yield TextDelta("must not request")
        agent = CodingAgent(Provider(), AsyncRegistry(), plan_enabled=True)
        turn = await agent.run_with_context_async("task", SessionContext(), cancellation=token)
        self.assertEqual([], started)
        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)

    async def test_cancellation_during_planning_returns_cancelled_result(self) -> None:
        """规划流被取消时应正常收尾，而不是把 CancellationError 泄漏给宿主。"""
        token = CancellationToken()

        class PlanningCancellationProvider:
            async def stream(self, messages, tools=(), *, cancellation=None):
                token.cancel()
                cancellation.raise_if_cancelled()
                if False:
                    yield ProviderCompleted("stop")

        agent = CodingAgent(
            PlanningCancellationProvider(),  # type: ignore[arg-type]
            AsyncRegistry(),  # type: ignore[arg-type]
        )
        observed: list[object] = []

        turn = await agent.run_with_context_async(
            "task",
            SessionContext(),
            cancellation=token,
            event_sink=observed.append,
        )

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertEqual(0, turn.result.rounds)
        self.assertIsInstance(observed[-1], RuntimeFailed)
        self.assertEqual("cancelled", observed[-1].category)

    async def test_explicit_token_cancel_at_create_approval_prevents_late_write(self) -> None:
        """父令牌在审批等待期间取消后，真实创建工具不得越过提交边界。"""
        import threading
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.tools import ToolContext, ToolRegistry

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "late-explicit.py"
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()
            release = threading.Event()

            def approve(action: str, _detail: str) -> bool:
                self.assertEqual("create_file", action)
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("approval release missing")
                return True

            registry = ToolRegistry(
                ToolContext(WorkspacePolicy(root), CommandPolicy(root), approve)
            )
            provider = HybridProvider([
                ProviderResponse(
                    tool_calls=(ToolCall(
                        "create-1",
                        "create_file",
                        {"path": target.name, "content": "must not be written\n"},
                    ),),
                    finish_reason="tool_calls",
                ),
            ])
            agent = CodingAgent(provider, registry, plan_enabled=False)
            parent = CancellationToken()
            task = asyncio.create_task(
                agent.run_with_context_async(
                    "create a file",
                    SessionContext(),
                    cancellation=parent,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(parent.cancel())
            finally:
                release.set()

            turn = await asyncio.wait_for(task, 2)

            self.assertFalse(turn.result.ok)
            self.assertEqual("任务已取消", turn.result.summary)
            self.assertFalse(target.exists())
            self.assertFalse(registry.has_pending_cleanup)

    async def test_native_task_cancel_at_create_approval_prevents_late_write(self) -> None:
        """原生 Task.cancel 必须取消本次子令牌，并原样返回给调用者。"""
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.tools import ToolContext, ToolRegistry

        with tempfile.TemporaryDirectory() as raw, ThreadPoolExecutor(max_workers=1) as executor:
            root = Path(raw)
            target = root / "late-native.py"
            loop = asyncio.get_running_loop()
            loop.set_default_executor(executor)
            entered = asyncio.Event()
            release = threading.Event()

            def approve(action: str, _detail: str) -> bool:
                self.assertEqual("create_file", action)
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("approval release missing")
                return True

            registry = ToolRegistry(
                ToolContext(WorkspacePolicy(root), CommandPolicy(root), approve)
            )
            provider = HybridProvider([
                ProviderResponse(
                    tool_calls=(ToolCall(
                        "create-1",
                        "create_file",
                        {"path": target.name, "content": "must not be written\n"},
                    ),),
                    finish_reason="tool_calls",
                ),
            ])
            agent = CodingAgent(provider, registry, plan_enabled=False)
            parent = CancellationToken()
            task = asyncio.create_task(
                agent.run_with_context_async(
                    "create a file",
                    SessionContext(),
                    cancellation=parent,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel("caller-native-cancel")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                self.assertIs(type(caught.exception), asyncio.CancelledError)
                self.assertEqual(("caller-native-cancel",), caught.exception.args)
                self.assertFalse(parent.is_cancelled, "取消本次调用不得反向污染父令牌")
                self.assertFalse(target.exists())
                self.assertTrue(registry.has_pending_cleanup)
            finally:
                release.set()
                await loop.run_in_executor(None, lambda: None)

            self.assertFalse(target.exists(), "调用者已收到取消后不得迟到创建文件")
            self.assertFalse(registry.has_pending_cleanup)

    async def test_sync_and_async_entrypoints_return_equivalent_results_and_audit_categories(self) -> None:
        """防止兼容包装与规范异步实现产生不同结果或审计状态。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            async_audit = root / "async.jsonl"
            sync_audit = root / "sync.jsonl"
            async_agent = CodingAgent(
                HybridProvider([_finish_response()]),
                AsyncRegistry(),  # type: ignore[arg-type]
                plan_enabled=False,
                audit=AuditLogger(async_audit),
            )
            sync_agent = CodingAgent(
                HybridProvider([_finish_response()]),
                AsyncRegistry(),  # type: ignore[arg-type]
                plan_enabled=False,
                audit=AuditLogger(sync_audit),
            )

            async_turn = await async_agent.run_with_context_async("task", SessionContext())
            sync_turn = await asyncio.to_thread(sync_agent.run_with_context, "task", SessionContext())

            self.assertEqual(async_turn, sync_turn)
            async_statuses = [json.loads(line)["status"] for line in async_audit.read_text(encoding="utf-8").splitlines()]
            sync_statuses = [json.loads(line)["status"] for line in sync_audit.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(async_statuses, sync_statuses)

    async def test_typed_events_follow_provider_policy_tool_and_completion_order(self) -> None:
        """防止工具在完整调用、审批事件或执行开始事件之前运行。"""
        call = ToolCall("inspect-1", "inspect", {})
        provider = HybridProvider([
            ProviderResponse(content="checking", tool_calls=(call,), finish_reason="tool_calls", usage=TokenUsage(4, 1)),
            _finish_response(),
        ])
        registry = AsyncRegistry()
        observed: list[object] = []
        agent = CodingAgent(provider, registry, plan_enabled=False)  # type: ignore[arg-type]

        turn = await agent.run_with_context_async("task", SessionContext(), event_sink=observed.append)

        self.assertTrue(turn.result.ok)
        event_types = [type(event) for event in observed]
        expected_in_order = [
            RoundStarted,
            TextDelta,
            ToolCallCompleted,
            UsageReported,
            ProviderCompleted,
            ApprovalRequested,
            ToolExecutionStarted,
            ToolExecutionCompleted,
            RoundStarted,
            ToolCallCompleted,
            UsageReported,
            ProviderCompleted,
            ToolExecutionStarted,
            ToolExecutionCompleted,
            RuntimeCompleted,
        ]
        self.assertEqual(expected_in_order, event_types)
        self.assertEqual(["inspect", "finish"], registry.calls)

    async def test_cancellation_between_tool_calls_completes_the_protocol_round(self) -> None:
        """防止取消在多工具中间留下孤立 assistant 调用或执行后续工具。"""
        token = CancellationToken()
        calls = (
            ToolCall("inspect-1", "inspect", {}),
            ToolCall("finish-1", "finish", {"summary": "must-not-run"}),
        )
        registry = AsyncRegistry(cancel_after_first=token)
        agent = CodingAgent(
            HybridProvider([ProviderResponse(tool_calls=calls, finish_reason="tool_calls")]),
            registry,  # type: ignore[arg-type]
            plan_enabled=False,
        )
        observed: list[object] = []

        turn = await agent.run_with_context_async(
            "task",
            SessionContext(),
            cancellation=token,
            event_sink=observed.append,
        )

        self.assertFalse(turn.result.ok)
        self.assertEqual(["inspect"], registry.calls)
        self.assertEqual(["assistant", "tool", "tool"], [message.role for message in turn.context.messages[-3:]])
        self.assertIsInstance(observed[-1], RuntimeFailed)

    async def test_sync_entrypoint_rejects_nested_event_loop(self) -> None:
        """防止同步包装在已有事件循环中调用 asyncio.run。"""
        agent = CodingAgent(HybridProvider([_finish_response()]), AsyncRegistry(), plan_enabled=False)  # type: ignore[arg-type]

        with self.assertRaises(RuntimeError):
            agent.run_with_context("task", SessionContext())


if __name__ == "__main__":
    unittest.main()
