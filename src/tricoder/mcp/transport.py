"""独立编排 stdio，并分别记录进程退出证据与本地资源关闭结果。"""

from __future__ import annotations

import asyncio
from asyncio import sleep
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any, Awaitable, Callable, TextIO


# 最坏清理预算：flush 0.5 + 自然退出 2 + 终止 2.25 + reap 2 +
# 并行关闭 0.5 + 任务回收 0.25 + 调度余量 1 = 8.5 秒，小于 client 的 10 秒。
_WRITER_FLUSH_SECONDS = 0.5
_NATURAL_EXIT_SECONDS = 2.0
_TERMINATE_SECONDS = 2.25
_REAP_SECONDS = 2.0
_RESOURCE_CLOSE_SECONDS = 0.5
_TASK_REAP_SECONDS = 0.25
_POLL_SECONDS = 0.01


class MCPProcessExitEvidence(str, Enum):
    NOT_STARTED = "not_started"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MCPTransportOutcome:
    process_exit: MCPProcessExitEvidence
    resources_closed: bool


@dataclass(frozen=True, slots=True)
class MCPStdioBindings:
    create_process: Callable[[Any, TextIO], Awaitable[Any]]
    terminate_process_tree: Callable[[Any], Awaitable[None]]
    close_process_job: Callable[[Any], bool]
    close_subprocess_transport: Callable[[Any], bool]
    create_memory_object_stream: Callable[[int], tuple[Any, Any]]
    parse_message: Callable[[str], Any]
    closed_resource_errors: tuple[type[BaseException], ...]
    end_of_stream_errors: tuple[type[BaseException], ...] = ()


class VerifiedStdioTransport:
    """只拥有自身进程、管道、内存流和任务的单次使用上下文。"""

    def __init__(
        self,
        parameters: Any,
        *,
        errlog: TextIO,
        bindings: MCPStdioBindings,
    ) -> None:
        self._parameters = parameters
        self._errlog = errlog
        self._bindings = bindings
        self._outcome = MCPTransportOutcome(MCPProcessExitEvidence.NOT_STARTED, False)
        self._process: Any | None = None
        self._streams: list[Any] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._writer: asyncio.Task[Any] | None = None
        self._cleanup: asyncio.Task[Any] | None = None
        self._entered = False
        self._faulted = False
        self._closing_resources = False

    @property
    def outcome(self) -> MCPTransportOutcome:
        """返回不可变快照；本地资源关闭不能提升进程退出证据。"""

        return self._outcome

    async def __aenter__(self) -> tuple[Any, Any]:
        if self._entered:
            raise RuntimeError("mcp_stdio_already_entered")
        self._entered = True
        try:
            incoming_send, incoming_receive = self._bindings.create_memory_object_stream(0)
            self._streams.extend((incoming_send, incoming_receive))
            outgoing_send, outgoing_receive = self._bindings.create_memory_object_stream(0)
            self._streams.extend((outgoing_send, outgoing_receive))
            self._process = await self._bindings.create_process(self._parameters, self._errlog)
            self._outcome = MCPTransportOutcome(MCPProcessExitEvidence.UNKNOWN, False)
            if self._process.stdin is None or self._process.stdout is None:
                raise RuntimeError("mcp_stdio_missing_pipe")
            self._spawn(self._bridge(self._read_messages(incoming_send)))
            self._writer = self._spawn(self._bridge(self._write_messages(outgoing_receive)))
            return incoming_receive, outgoing_send
        except BaseException as primary:
            await self.__aexit__(type(primary), primary, primary.__traceback__)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        # 独立 cleanup task 不继承调用者的原生取消；反复取消也不重启清理预算。
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._shutdown())
        cancellation: asyncio.CancelledError | None = None
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError as cancelled:
                if cancellation is None:
                    cancellation = cancelled
        try:
            self._cleanup.result()
        except BaseException:
            if exc is None:
                raise
        if exc is None and cancellation is not None:
            raise cancellation

    async def _bridge(self, operation: Awaitable[Any]) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise  # 受控关闭取消不是 I/O 失败；flush 超时由调用点单独记录。
        except BaseException:
            self._faulted = True
            raise

    def _spawn(self, operation: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        # 超时任务仍保留精确引用；即使其延迟结束也会消费异常。
        task.add_done_callback(_consume_result)
        return task

    async def _read_messages(self, destination: Any) -> None:
        suffix = b""
        try:
            while True:
                chunk = await self._process.stdout.receive()
                if not chunk:
                    break
                suffix += chunk
                while b"\n" in suffix:
                    line, suffix = suffix.split(b"\n", 1)
                    try:
                        message = self._bindings.parse_message(
                            line.decode("utf-8", errors="replace")
                        )
                    except Exception:
                        message = RuntimeError("mcp_stdio_invalid_message")
                    if isinstance(message, BaseException):
                        # 不格式化验证异常，其内部可能保存原始 JSON 或敏感字段。
                        message = RuntimeError("mcp_stdio_invalid_message")
                    await destination.send(message)
        except self._bindings.end_of_stream_errors:
            pass
        except self._bindings.closed_resource_errors:
            if not self._closing_resources:
                raise
        finally:
            await destination.aclose()

    async def _write_messages(self, source: Any) -> None:
        try:
            while True:
                session_message = await source.receive()
                payload = session_message.message.model_dump_json(
                    by_alias=True, exclude_unset=True
                )
                await self._process.stdin.send((payload + "\n").encode("utf-8"))
        except self._bindings.end_of_stream_errors:
            pass
        except self._bindings.closed_resource_errors:
            if not self._closing_resources:
                raise

    async def _wait_for_returncode(self, seconds: float) -> bool:
        deadline = monotonic() + seconds
        while self._process.returncode is None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            await sleep(min(_POLL_SECONDS, remaining))
        return True

    async def _complete_within(self, operation: Awaitable[Any], seconds: float) -> bool:
        task = self._spawn(operation)
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if task not in done:
            task.cancel()
            return False
        return task.result() is not False

    async def _prove_process_exit(self) -> MCPProcessExitEvidence:
        if self._process.returncode is not None:
            return MCPProcessExitEvidence.VERIFIED
        if await self._wait_for_returncode(_NATURAL_EXIT_SECONDS):
            return MCPProcessExitEvidence.VERIFIED
        try:
            # 额外限制注入 helper 的墙钟时间，不把终止信号发送成功当作退出证据。
            if not await self._complete_within(
                self._bindings.terminate_process_tree(self._process), _TERMINATE_SECONDS
            ):
                return MCPProcessExitEvidence.UNKNOWN
        except BaseException:
            return MCPProcessExitEvidence.UNKNOWN
        if await self._wait_for_returncode(_REAP_SECONDS):
            return MCPProcessExitEvidence.VERIFIED
        return MCPProcessExitEvidence.UNKNOWN

    async def _close(self, resource: Any) -> bool:
        try:
            await resource.aclose()
            return True
        except BaseException:
            # 读写结束或损坏异常不能证明端点已关闭；关闭异常一律保守判为失败。
            return False

    async def _flush_writer(self) -> None:
        # 关闭生产端允许 writer 消费已接收的消息；共享一个 flush 截止时间。
        deadline = asyncio.get_running_loop().time() + _WRITER_FLUSH_SECONDS
        if not await self._complete_within(self._close(self._streams[2]), _WRITER_FLUSH_SECONDS):
            self._faulted = True
        if self._writer is not None:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait({self._writer}, timeout=remaining)
            if not self._writer.done():
                self._faulted = True
                self._writer.cancel()
        if self._process.stdin is not None:
            # stdin 关闭也属于 flush 预算；即使失败，最终关闭路径仍会再次尝试。
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            if not await self._complete_within(self._close(self._process.stdin), remaining):
                self._faulted = True

    async def _shutdown(self) -> None:
        evidence = self._outcome.process_exit
        try:
            if self._process is not None:
                await self._flush_writer()
                evidence = await self._prove_process_exit()
        except BaseException:
            if self._process is not None:
                evidence = MCPProcessExitEvidence.UNKNOWN
        finally:
            closed = await self._close_owned_resources()
            if (not closed or self._faulted) and self._process is not None:
                evidence = MCPProcessExitEvidence.UNKNOWN
            self._outcome = MCPTransportOutcome(evidence, closed)

    async def _close_owned_resources(self) -> bool:
        self._closing_resources = True
        resources = list(self._streams)
        if self._process is not None:
            resources.extend(
                pipe for pipe in (self._process.stdin, self._process.stdout) if pipe is not None
            )
        closing = {self._spawn(self._close(resource)) for resource in resources}
        closed = True
        if closing:
            done, pending = await asyncio.wait(closing, timeout=_RESOURCE_CLOSE_SECONDS)
            closed = not pending and all(task.result() for task in done)
        if self._process is not None:
            for close in (
                self._bindings.close_process_job,
                self._bindings.close_subprocess_transport,
            ):
                try:
                    if close(self._process) is not True:
                        closed = False
                except BaseException:
                    closed = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=_TASK_REAP_SECONDS)
            closed = closed and not pending
        return closed


def _consume_result(task: asyncio.Task[Any]) -> None:
    """只消费本 transport 创建任务的结果，不查询全局任务集合。"""

    if not task.cancelled():
        task.exception()
