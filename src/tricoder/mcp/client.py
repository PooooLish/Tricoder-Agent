"""单个 MCP stdio server 的异步客户端生命周期。"""

from __future__ import annotations

import asyncio
import copy
import os
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, TextIO, TypeVar

from tricoder.core.cancellation import CancellationError, CancellationToken

from .models import MCPCallResult, MCPServerState, MCPToolSpec
from .schema import validate_mcp_schema
from .sdk import MCPDependencyError, MCPSDK, SDKLogSource, isolate_sdk_logs, load_mcp_sdk
from .security import MCPLaunchRequest
from .tool_adapter import normalize_mcp_result, normalize_tool_name
from .transport import MCPProcessExitEvidence, VerifiedStdioTransport


_T = TypeVar("_T")
_POLL_INTERVAL_SECONDS = 0.05
_OPERATION_REAP_TIMEOUT_SECONDS = 0.5
# 本地 transport 的最坏预算为 8.5 秒；另留 session 退出及调度余量。
_DEFAULT_CLEANUP_TIMEOUT_SECONDS = 10.0
# 无法强制终止吞掉 CancelledError 的 Python 协程；保留强引用并在其最终完成时
# 消费异常，避免调用方越过墙钟预算等待，也避免 “Task exception was never retrieved”。
_DEFERRED_REAP_TASKS: set[asyncio.Task[Any]] = set()


class MCPClientError(RuntimeError):
    """MCP 客户端的稳定错误基类。"""


class MCPTimeoutError(MCPClientError):
    """MCP 请求超过本地时间预算。"""


class MCPProtocolError(MCPClientError):
    """MCP transport 或协议对象不符合客户端契约。"""


class MCPCleanupError(MCPClientError):
    """MCP 资源无法确认已经完整回收。"""


class _MCPDeferredCancellation(CancellationError):
    """显式取消已发生，但被取消 operation 尚未在预算内结束。"""


class MCPClient:
    """拥有一个已审批 MCP stdio server 的 transport 与 session。"""

    def __init__(
        self,
        server_id: str,
        launch_request: MCPLaunchRequest,
        *,
        initialize_timeout: float = 10.0,
        operation_timeout: float = 30.0,
        cleanup_timeout: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
        sdk_loader: Callable[[], MCPSDK] = load_mcp_sdk,
        transport_factory: Callable[..., VerifiedStdioTransport] = VerifiedStdioTransport,
    ) -> None:
        if not isinstance(server_id, str) or not server_id:
            raise ValueError("server_id 必须是非空字符串")
        if not isinstance(launch_request, MCPLaunchRequest):
            raise TypeError("launch_request 必须是 MCPLaunchRequest")
        for name, value in (
            ("initialize_timeout", initialize_timeout),
            ("operation_timeout", operation_timeout),
            ("cleanup_timeout", cleanup_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} 必须是正数")
        if not callable(sdk_loader):
            raise TypeError("sdk_loader 必须可调用")
        if not callable(transport_factory):
            raise TypeError("transport_factory 必须可调用")

        self.server_id = server_id
        self._launch_request = launch_request
        self._initialize_timeout = float(initialize_timeout)
        self._operation_timeout = float(operation_timeout)
        self._cleanup_timeout = float(cleanup_timeout)
        self._sdk_loader = sdk_loader
        self._transport_factory = transport_factory
        self._state = MCPServerState.STOPPED
        self._session: Any | None = None
        self._log_sources: tuple[SDKLogSource, ...] = ()
        self._log_operation_tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._stop_requested: asyncio.Event | None = None
        self._lifecycle_failure: BaseException | None = None
        self._lifecycle_abandoned = False
        # owner 结束不等于资源已回收；退出失败必须在释放 task 引用后仍可观察。
        self._cleanup_failed = False

    @property
    def state(self) -> MCPServerState:
        """返回不包含 SDK 或 transport 细节的生命周期状态。"""

        return self._state

    async def start(self, cancellation: CancellationToken) -> None:
        """建立 stdio transport、进入 session，并完成一次 initialize。"""

        cancellation.raise_if_cancelled()
        if self._state is not MCPServerState.STOPPED:
            raise MCPClientError("mcp_client_invalid_state")

        self._state = MCPServerState.STARTING
        self._lifecycle_failure = None
        self._lifecycle_abandoned = False
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._stop_requested = asyncio.Event()
        lifecycle_task = asyncio.create_task(
            self._run_lifecycle(ready, self._stop_requested)
        )
        self._lifecycle_task = lifecycle_task
        lifecycle_task.add_done_callback(self._on_lifecycle_done)
        try:
            await _wait_for_start_ready(
                ready,
                timeout=self._initialize_timeout,
                cancellation=cancellation,
            )
        except CancellationError:
            # 显式 token 取消必须保持可识别；即使清理失败也不能改写该主错误。
            try:
                await self._abort_start_owner(ready, lifecycle_task)
            except asyncio.CancelledError:
                pass  # 后续原生取消不替换已捕获的 token 主异常。
            self._state = MCPServerState.FAILED
            raise
        except MCPTimeoutError as primary_timeout:
            try:
                owner_finished = await self._abort_start_owner(ready, lifecycle_task)
            except asyncio.CancelledError:
                self._state = MCPServerState.FAILED
                raise primary_timeout
            self._state = MCPServerState.FAILED
            if not owner_finished or self._cleanup_failed or isinstance(
                self._lifecycle_failure,
                MCPCleanupError,
            ):
                raise MCPCleanupError("mcp_cleanup_failed") from None
            raise
        except asyncio.CancelledError:
            # ready 是独立 Future；取消 owner，让它在原 task 内清理。
            cancellation.cancel()
            try:
                await self._abort_start_owner(ready, lifecycle_task)
            except asyncio.CancelledError:
                pass  # owner 回调/延迟收割仍持有任务，原始取消身份优先。
            self._state = MCPServerState.FAILED
            raise
        except BaseException:
            # 生命周期 task 在设置失败结果前已经完成其同 task 清理。
            try:
                owner_finished = await _reap_task(lifecycle_task)
            except asyncio.CancelledError:
                owner_finished = False
            if owner_finished:
                self._lifecycle_task = None
                self._stop_requested = None
            raise

    async def list_tools(
        self,
        cancellation: CancellationToken,
    ) -> tuple[MCPToolSpec, ...]:
        """获取、验证并复制工具定义，不向调用方暴露 SDK 对象。"""

        cancellation.raise_if_cancelled()
        session = self._ready_session()
        try:
            result = await _await_bounded(
                session.list_tools(),
                timeout=self._operation_timeout,
                cancellation=cancellation,
                timeout_code="mcp_list_tools_timeout",
                log_sources=self._log_sources,
                track_operation=self._track_log_operation,
            )
            tools = _read_field(result, "tools")
            if not isinstance(tools, (list, tuple)):
                raise ValueError("invalid tool collection")
            specs: list[MCPToolSpec] = []
            for tool in tools:
                raw_name = _read_field(tool, "name")
                description = _read_field(tool, "description")
                raw_schema = _read_field(tool, "inputSchema")
                if raw_schema is None:
                    raw_schema = _read_field(tool, "input_schema")
                if not isinstance(raw_name, str):
                    raise ValueError("invalid tool name")
                if description is None:
                    description = ""
                if not isinstance(description, str):
                    raise ValueError("invalid tool description")
                schema = validate_mcp_schema(raw_schema)
                specs.append(
                    MCPToolSpec(
                        server_id=self.server_id,
                        raw_name=raw_name,
                        public_name=normalize_tool_name(self.server_id, raw_name),
                        description=description,
                        input_schema=schema,
                    )
                )
            return tuple(specs)
        except _MCPDeferredCancellation:
            # 延迟存活的请求使 session 状态不可再信任，但 Task 6 仍需识别取消。
            self._state = MCPServerState.FAILED
            raise CancellationError("操作已取消") from None
        except asyncio.CancelledError:
            # 调用者取消发生时，底层请求是否已经停止可能未知，禁止继续复用 session。
            self._state = MCPServerState.FAILED
            raise
        except MCPCleanupError:
            # operation 未在回收预算内停止，session 的请求边界已不再可信。
            self._state = MCPServerState.FAILED
            raise
        except (CancellationError, MCPClientError):
            raise
        except Exception:
            raise MCPProtocolError("mcp_protocol_error") from None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult:
        """按原始名称调用工具，并将结果归一化为内部不可变模型。"""

        cancellation.raise_if_cancelled()
        session = self._ready_session()
        try:
            copied_arguments = copy.deepcopy(arguments)
            result = await _await_bounded(
                session.call_tool(name, copied_arguments),
                timeout=self._operation_timeout,
                cancellation=cancellation,
                timeout_code="mcp_tool_timeout",
                log_sources=self._log_sources,
                track_operation=self._track_log_operation,
            )
            return normalize_mcp_result(result)
        except _MCPDeferredCancellation:
            self._state = MCPServerState.FAILED
            raise CancellationError("操作已取消") from None
        except asyncio.CancelledError:
            self._state = MCPServerState.FAILED
            raise
        except MCPCleanupError:
            self._state = MCPServerState.FAILED
            raise
        except (CancellationError, MCPClientError):
            raise
        except Exception:
            raise MCPProtocolError("mcp_protocol_error") from None

    async def stop(self) -> None:
        """反向退出 session/transport；重复调用不会产生额外副作用。"""

        lifecycle_task = self._lifecycle_task
        if self._state is MCPServerState.STOPPED:
            if lifecycle_task is not None and lifecycle_task.done():
                self._on_lifecycle_done(lifecycle_task)
            return
        if self._state is MCPServerState.FAILED and (
            lifecycle_task is None or lifecycle_task.done()
        ):
            self._lifecycle_task = None
            self._stop_requested = None
            if self._cleanup_failed:
                raise MCPCleanupError("mcp_cleanup_failed")
            return
        if lifecycle_task is None or self._stop_requested is None:
            self._state = MCPServerState.FAILED
            raise MCPCleanupError("mcp_cleanup_failed")

        self._state = MCPServerState.STOPPING
        self._stop_requested.set()
        cleanup_deadline = asyncio.get_running_loop().time() + self._cleanup_timeout
        try:
            await asyncio.wait_for(
                asyncio.shield(lifecycle_task),
                timeout=self._cleanup_timeout,
            )
        except asyncio.CancelledError as cancellation:
            # owner task 未被取消，会继续在进入上下文的同一 task 内完成清理。
            try:
                try:
                    # 取消后的等待只使用原 deadline 的余量，不能重新给出完整预算。
                    await asyncio.wait_for(
                        asyncio.shield(lifecycle_task),
                        timeout=max(0.0, cleanup_deadline - asyncio.get_running_loop().time()),
                    )
                except Exception:
                    self._lifecycle_abandoned = True
                    self._cleanup_failed = True
                    lifecycle_task.cancel()
                    self._state = MCPServerState.FAILED
                    await _reap_task(lifecycle_task)
            except asyncio.CancelledError:
                # 再次取消可中断等待/收割，但不能替换首个主异常；owner 回调和
                # _reap_tasks 的延迟消费仍拥有未结束任务，manager 也保留非 STOPPED 项。
                pass
            raise cancellation
        except TimeoutError:
            self._lifecycle_abandoned = True
            self._cleanup_failed = True
            lifecycle_task.cancel()
            await _reap_task(lifecycle_task)
            self._state = MCPServerState.FAILED
            raise MCPCleanupError("mcp_cleanup_failed") from None
        except MCPCleanupError:
            self._cleanup_failed = True
            self._state = MCPServerState.FAILED
            raise
        except Exception:
            self._cleanup_failed = True
            self._state = MCPServerState.FAILED
            raise MCPCleanupError("mcp_cleanup_failed") from None
        finally:
            if lifecycle_task.done():
                self._lifecycle_task = None
                self._stop_requested = None

    def _ready_session(self) -> Any:
        if self._state is not MCPServerState.READY or self._session is None:
            raise MCPClientError("mcp_client_not_ready")
        return self._session

    def _on_lifecycle_done(self, task: asyncio.Task[None]) -> None:
        """只清理由本 client 创建且仍为当前实例所拥有的 exact task。"""

        _consume_task_result(task)
        if self._lifecycle_task is task:
            self._lifecycle_task = None
            self._stop_requested = None
        self._release_log_sources_if_reaped()

    def _track_log_operation(self, task: asyncio.Task[Any]) -> None:
        """仅跟踪已创建请求的日志归属，不改变取消/收割顺序。"""
        self._log_operation_tasks.add(task)
        task.add_done_callback(self._on_log_operation_done)

    def _on_log_operation_done(self, task: asyncio.Task[Any]) -> None:
        self._log_operation_tasks.discard(task)
        self._release_log_sources_if_reaped()

    def _release_log_sources_if_reaped(self) -> None:
        owner = self._lifecycle_task
        if (owner is None or owner.done()) and not self._log_operation_tasks:
            self._log_sources = ()

    async def _run_lifecycle(
        self,
        ready: asyncio.Future[None],
        stop_requested: asyncio.Event,
    ) -> None:
        """在唯一 owner task 中进入和退出 SDK/AnyIO 上下文。"""

        stack = AsyncExitStack()
        stderr_sink: TextIO | None = None
        phase = "transport"
        primary_error: BaseException | None = None
        cleanup_error: MCPCleanupError | None = None
        announced_ready = False
        transport: VerifiedStdioTransport | None = None
        transport_entered = False
        try:
            sdk = self._sdk_loader()
            self._log_sources = sdk.log_sources
            stack.enter_context(isolate_sdk_logs(self._log_sources))

            parameters = sdk.stdio_server_parameters(
                command=self._launch_request.command,
                args=list(self._launch_request.args),
                env=dict(self._launch_request.env),
                cwd=str(self._launch_request.cwd),
                encoding_error_handler="replace",
            )
            stderr_sink = open(  # noqa: PTH123 - os.devnull 是明确的系统 null sink。
                os.devnull,
                "w",
                encoding="utf-8",
                errors="replace",
            )
            await stack.__aenter__()
            transport = self._transport_factory(
                parameters, errlog=stderr_sink, bindings=sdk.stdio_bindings,
            )
            read_stream, write_stream = await stack.enter_async_context(
                transport
            )
            transport_entered = True
            session = await stack.enter_async_context(
                sdk.client_session(read_stream, write_stream)
            )
            self._session = session
            phase = "protocol"
            # 整个 enter + initialize 由 start 外层的单一墙钟预算约束；这里不再
            # 叠加第二个 deadline，避免两个相同超时形成调度竞态。
            await session.initialize()
            self._state = MCPServerState.READY
            announced_ready = True
            if not ready.done():
                ready.set_result(None)
            await stop_requested.wait()
        except BaseException as exc:
            primary_error = _normalize_lifecycle_error(exc, phase=phase)
        finally:
            self._session = None
            try:
                async with asyncio.timeout(self._cleanup_timeout):
                    await stack.aclose()
                if transport is not None:
                    outcome = transport.outcome
                    # 成功 enter 证明已经启动；部分 enter 失败则使用 transport 留下的证据。
                    process_was_started = transport_entered or (
                        outcome.process_exit is not MCPProcessExitEvidence.NOT_STARTED
                    )
                    transport_verified = (
                        outcome.process_exit is MCPProcessExitEvidence.VERIFIED
                        and outcome.resources_closed
                    )
                    if not outcome.resources_closed or (process_was_started and not transport_verified):
                        self._cleanup_failed = True
                        cleanup_error = MCPCleanupError("mcp_cleanup_failed")
            except BaseException:
                self._cleanup_failed = True
                cleanup_error = MCPCleanupError("mcp_cleanup_failed")
            finally:
                if stderr_sink is not None:
                    stderr_sink.close()

        if not announced_ready:
            self._state = MCPServerState.FAILED
            if isinstance(primary_error, (CancellationError, asyncio.CancelledError)):
                failure = primary_error
            else:
                failure = cleanup_error or primary_error or MCPProtocolError(
                    "mcp_transport_error"
                )
            self._lifecycle_failure = failure
            if not ready.done():
                ready.set_exception(failure)
            return

        if isinstance(primary_error, (CancellationError, asyncio.CancelledError)):
            self._state = MCPServerState.FAILED
            raise primary_error
        if cleanup_error is not None:
            self._state = MCPServerState.FAILED
            raise cleanup_error from None
        if primary_error is not None:
            self._state = MCPServerState.FAILED
            if isinstance(primary_error, asyncio.CancelledError):
                raise primary_error
            if isinstance(primary_error, MCPClientError):
                raise primary_error from None
            raise MCPCleanupError("mcp_cleanup_failed") from None
        self._state = (
            MCPServerState.FAILED
            if self._lifecycle_abandoned or self._cleanup_failed
            else MCPServerState.STOPPED
        )

    async def _abort_start_owner(
        self,
        ready: asyncio.Future[None],
        lifecycle_task: asyncio.Task[None],
    ) -> bool:
        """取消启动 owner，并在真实墙钟预算内等待其同 task 清理。"""

        ready.cancel()
        lifecycle_task.cancel()
        owner_finished = await _reap_tasks(
            {lifecycle_task},
            timeout=self._cleanup_timeout + _OPERATION_REAP_TIMEOUT_SECONDS,
        )
        if owner_finished:
            self._lifecycle_task = None
            self._stop_requested = None
        else:
            self._lifecycle_abandoned = True
            self._cleanup_failed = True
        return owner_finished


async def _await_bounded(
    operation: Awaitable[_T],
    *,
    timeout: float,
    cancellation: CancellationToken,
    timeout_code: str,
    log_sources: tuple[SDKLogSource, ...],
    track_operation: Callable[[asyncio.Task[Any]], None],
) -> _T:
    """在事件循环内竞争请求、超时与协作式取消，并收割内部任务。"""

    operation_task = asyncio.create_task(_run_sdk_operation(operation, log_sources))
    track_operation(operation_task)
    cancellation_task = asyncio.create_task(_wait_for_cancellation(cancellation))
    token_cancelled = False
    caller_cancellation: asyncio.CancelledError | None = None
    try:
        done, _ = await asyncio.wait(
            {operation_task, cancellation_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            operation_task.cancel()
            raise MCPTimeoutError(timeout_code)
        if cancellation_task in done:
            token_cancelled = True
            operation_task.cancel()
            raise CancellationError("操作已取消")
        return await operation_task
    except asyncio.CancelledError as exc:
        # SDK 请求可能先于整个任务 scope 进入 finally，同步发布协作式取消。
        caller_cancellation = exc
        cancellation.cancel()
        raise
    finally:
        cancellation_task.cancel()
        if not operation_task.done():
            operation_task.cancel()
        try:
            reaped = await _reap_tasks(
                {operation_task, cancellation_task},
                timeout=_OPERATION_REAP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if caller_cancellation is not None:
                raise caller_cancellation
            raise
        if not reaped:
            if caller_cancellation is not None:
                raise caller_cancellation
            if token_cancelled:
                raise _MCPDeferredCancellation("操作已取消") from None
            raise MCPCleanupError("mcp_cleanup_failed") from None


async def _run_sdk_operation(
    operation: Awaitable[_T], log_sources: tuple[SDKLogSource, ...],
) -> _T:
    """请求 task 与生命周期 owner 分离，必须各自建立日志保护作用域。"""

    with isolate_sdk_logs(log_sources):
        return await operation


async def _wait_for_start_ready(
    ready: asyncio.Future[None],
    *,
    timeout: float,
    cancellation: CancellationToken,
) -> None:
    """用一个 deadline 覆盖 transport/session enter 与 initialize。"""

    cancellation_task = asyncio.create_task(_wait_for_cancellation(cancellation))
    primary_error: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            {ready, cancellation_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            raise CancellationError("操作已取消")
        if ready in done:
            return ready.result()
        raise MCPTimeoutError("mcp_initialize_timeout")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cancellation_task.cancel()
        try:
            await _reap_tasks(
                {cancellation_task},
                timeout=_OPERATION_REAP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if primary_error is None:
                raise


async def _wait_for_cancellation(cancellation: CancellationToken) -> None:
    """使用短周期异步轮询连接现有线程安全 CancellationToken。"""

    while True:
        cancellation.raise_if_cancelled()
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _reap_task(task: asyncio.Task[Any]) -> bool:
    """在真实墙钟预算内收割一个 task；返回是否已经完成。"""

    return await _reap_tasks(
        {task},
        timeout=_OPERATION_REAP_TIMEOUT_SECONDS,
    )


async def _reap_tasks(
    tasks: set[asyncio.Task[Any]],
    *,
    timeout: float,
) -> bool:
    """有限等待已取消 task，不使用会等待取消完成的 wait_for。"""

    if not tasks:
        return True
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except asyncio.CancelledError:
        # 收割等待者本身也可能被调用方取消。此时仍须为每个 operation 安排
        # 最终异常消费，否则正常竞态会触发 “Task exception was never retrieved”。
        for task in tasks:
            if task.done():
                _consume_task_result(task)
            else:
                _track_deferred_reap(task)
        raise
    for task in done:
        _consume_task_result(task)
    for task in pending:
        _track_deferred_reap(task)
    return not pending


def _track_deferred_reap(task: asyncio.Task[Any]) -> None:
    """保留未协作 task，并在其最终完成时安全消费异常。"""

    if task.done():
        _consume_task_result(task)
        return
    if task in _DEFERRED_REAP_TASKS:
        return
    _DEFERRED_REAP_TASKS.add(task)
    task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    _DEFERRED_REAP_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


def _normalize_lifecycle_error(
    error: BaseException,
    *,
    phase: str,
) -> BaseException:
    """保留本地固定异常；其余 transport/SDK 异常只映射为稳定类别。"""

    if isinstance(
        error,
        (CancellationError, asyncio.CancelledError, MCPDependencyError, MCPClientError),
    ):
        return error
    code = "mcp_protocol_error" if phase == "protocol" else "mcp_transport_error"
    return MCPProtocolError(code)


def _read_field(value: object, name: str) -> object:
    """读取受信字段名，失败时不格式化未知 SDK 对象。"""

    if isinstance(value, dict):
        try:
            return value.get(name)
        except Exception:
            return None
    try:
        return getattr(value, name)
    except Exception:
        return None
