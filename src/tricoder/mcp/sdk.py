"""官方 MCP SDK 的按需导入边界。"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from typing import Any, TextIO

from .transport import MCPStdioBindings


# 只保护当前 task 的 exact logger + 词法源码身份，不修改宿主日志配置。
_SDK_LOG_SCOPE: ContextVar[frozenset[tuple[str, str]]] = ContextVar(
    "tricoder_mcp_log_scope", default=frozenset(),
)
_SDK_LOG_LOCK = RLock()
_SDK_LOG_USERS: dict[str, int] = {}
_SDK_LOG_VIEWS: dict[str, _HostFilterView] = {}
# 强引用保留未关闭的 exact handle，失败不能因生命周期 owner 结束而被遗忘。
_OWNED_PROCESS_JOBS: dict[Any, Any] = {}


class _SDKLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        scope = _SDK_LOG_SCOPE.get()
        # LogRecord 正常携带绝对路径；相对或无效来源不等同已验证 SDK 身份。
        # 热路径只做字符串运算，不 resolve/stat/getcwd，也不格式化消息/异常。
        if not scope or not isinstance(record.pathname, str) or not os.path.isabs(record.pathname):
            return True
        return (record.name, os.path.normcase(os.path.normpath(record.pathname))) not in scope


_SDK_LOG_FILTER = _SDKLogFilter()


class _HostFilterView:
    """临时覆盖层；标准 addFilter/removeFilter 始终更新 exact 宿主 list。"""

    def __init__(self, host: list[Any]) -> None:
        self.host = host
        self.active = True

    def __iter__(self) -> Iterator[Any]:
        # 每次日志遍历持有稳定快照；安装/释放不移动在途遍历的索引。
        return iter(([_SDK_LOG_FILTER] if self.active else []) + self.host.copy())

    def __len__(self) -> int:
        return len(self.host) + int(self.active)

    def __getitem__(self, index: Any) -> Any:
        return tuple(self)[index]

    def __contains__(self, value: Any) -> bool:
        return (self.active and value is _SDK_LOG_FILTER) or value in self.host

    def append(self, value: Any) -> None:
        self.host.append(value)

    def remove(self, value: Any) -> None:
        self.host.remove(value)


@contextmanager
def isolate_sdk_logs(sources: tuple[SDKLogSource, ...]) -> Iterator[None]:
    """并发引用计数保护 SDK 原始日志，最后一个作用域退出时精确撤销。"""

    pairs = frozenset((source.logger_name, source.source_path) for source in sources)
    names = frozenset(name for name, _ in pairs)
    with _SDK_LOG_LOCK:
        for name in names:
            if not _SDK_LOG_USERS.get(name, 0):
                logger = logging.getLogger(name)
                view = _HostFilterView(logger.filters)
                _SDK_LOG_VIEWS[name] = view
                logger.filters = view
            _SDK_LOG_USERS[name] = _SDK_LOG_USERS.get(name, 0) + 1
    token = _SDK_LOG_SCOPE.set(_SDK_LOG_SCOPE.get() | pairs)
    try:
        yield
    finally:
        _SDK_LOG_SCOPE.reset(token)
        with _SDK_LOG_LOCK:
            for name in names:
                _SDK_LOG_USERS[name] -= 1
                if not _SDK_LOG_USERS[name]:
                    del _SDK_LOG_USERS[name]
                    logger = logging.getLogger(name)
                    view = _SDK_LOG_VIEWS.pop(name)
                    view.active = False
                    # 宿主整体替换容器时，新容器归宿主管理，不覆盖它。
                    if logger.filters is view:
                        logger.filters = view.host


class MCPDependencyError(RuntimeError):
    """启用 MCP 但官方 SDK 不可用。"""


@dataclass(frozen=True, slots=True)
class SDKLogSource:
    """锁定 SDK 的真实 logger 名称与规范化源码路径。"""

    logger_name: str
    source_path: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_path",
            os.path.normcase(os.path.abspath(os.path.normpath(self.source_path))),
        )


@dataclass(frozen=True, slots=True)
class MCPSDK:
    """隔离第三方 SDK 对象，避免它们扩散到 TriCoder 其他模块。"""

    client_session: type[Any]
    stdio_server_parameters: type[Any]
    stdio_bindings: MCPStdioBindings
    log_sources: tuple[SDKLogSource, ...]


def _require_shape(
    function: Any, *args: Any, asynchronous: bool = False,
    native_name: str | None = None, **kwargs: Any,
) -> None:
    """启动前检查实际调用的签名及 async/sync 约定，不试调用有副作用的能力。"""

    is_async = inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)
    )
    if not callable(function) or is_async != asynchronous:
        raise TypeError("mcp_sdk_capability_invalid")
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        # 锁定 pywin32 的两个 C API 不暴露 signature；只接受 exact native 名称。
        if native_name and inspect.isbuiltin(function) and function.__name__ == native_name:
            return
        raise TypeError("mcp_sdk_capability_signature_invalid") from None
    signature.bind(*args, **kwargs)


def _close_exact_subprocess_transport(process: Any) -> bool:
    """独立实现、绑定 mcp 2.1.1/AnyIO asyncio 对象形状；不使用吞异常的 SDK helper。"""

    asyncio_process = getattr(process, "_process", None)
    transport = getattr(asyncio_process, "_transport", None)
    if transport is None:
        # Windows Popen fallback 没有 asyncio transport，不持有该类资源。
        return True
    _require_shape(transport.close)
    transport.close()  # PermissionError 等必须向上传递，不能变成关闭证据。
    is_closing = getattr(transport, "is_closing", None)
    if is_closing is not None:
        _require_shape(is_closing)
        return is_closing() is True
    return True


def load_mcp_sdk() -> MCPSDK:
    """在真正启用 MCP 时才加载官方 SDK。"""

    try:
        from importlib.metadata import version

        import anyio
        import mcp_types
        from mcp import ClientSession, StdioServerParameters
        from mcp.client import session, stdio
        from mcp.os.posix import utilities as posix
        from mcp.os.win32 import utilities as windows
        from mcp.shared import dispatcher, jsonrpc_dispatcher
        from mcp.shared.message import SessionMessage

        if version("mcp") != "2.1.1" or version("mcp-types") != "2.1.1":
            raise ValueError("mcp_sdk_version_mismatch")

        # 先一次性验证，再把精确能力绑定到闭包；任何缺失都必须早于进程创建失败。
        capabilities = (
            anyio.open_process,
            anyio.create_memory_object_stream,
            mcp_types.jsonrpc_message_adapter.validate_json,
            windows.create_windows_process,
            windows.terminate_windows_process_tree,
            posix.terminate_posix_process_tree,
        )
        if not all(callable(capability) for capability in capabilities):
            raise TypeError("mcp_sdk_capability_invalid")
        (
            open_process, memory_streams, validate_json, windows_create,
            windows_terminate, posix_terminate,
        ) = capabilities
        _require_shape(open_process, ["approved"], env={}, cwd=None, stderr=None,
                       start_new_session=True, asynchronous=True)
        _require_shape(windows_create, "approved", [], env={}, errlog=None, cwd=None,
                       asynchronous=True)
        _require_shape(windows_terminate, object(), asynchronous=True)
        _require_shape(posix_terminate, object(), asynchronous=True)
        _require_shape(memory_streams, 0)
        _require_shape(validate_json, "{}", by_name=False)
        if not all(
            isinstance(value, type)
            for value in (ClientSession, StdioServerParameters, SessionMessage)
        ):
            raise TypeError("mcp_sdk_type_invalid")
        _require_shape(ClientSession, object(), object())
        _require_shape(StdioServerParameters, command="approved", args=[], env={},
                       cwd=None, encoding_error_handler="replace")
        _require_shape(SessionMessage, object())
        for name in ("__aenter__", "__aexit__", "initialize", "list_tools", "call_tool"):
            args = (None, None, None) if name == "__aexit__" else (
                ("tool", {}) if name == "call_tool" else ()
            )
            _require_shape(getattr(ClientSession, name), object(), *args, asynchronous=True)
        closed_errors = (
            anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError
        )
        if not all(
            isinstance(error, type) and issubclass(error, Exception)
            for error in closed_errors
        ):
            raise TypeError("mcp_sdk_exception_invalid")

        sources = []
        active_utility = windows if sys.platform == "win32" else posix
        for module in (stdio, session, jsonrpc_dispatcher, dispatcher, posix, windows):
            logger = getattr(module, "logger", None)
            pathname = getattr(module, "__file__", None)
            if not isinstance(logger, logging.Logger) or not isinstance(pathname, str) or not os.path.isabs(pathname):
                if module in (posix, windows) and module is not active_utility:
                    continue
                raise TypeError("mcp_sdk_log_source_invalid")
            sources.append(SDKLogSource(logger.name, pathname))

        platform = sys.platform
        jobs = None
        close_handle = terminate_job = None
        if platform == "win32":
            # 私有 mapping 仅支持已审查的 pinned-SDK exact WeakKeyDictionary。
            jobs = windows._process_jobs
            if type(jobs) is not weakref.WeakKeyDictionary:
                raise TypeError("mcp_sdk_job_mapping_invalid")
            close_handle = windows.win32api.CloseHandle
            terminate_job = windows.win32job.TerminateJobObject
            _require_shape(close_handle, object(), native_name="CloseHandle")
            _require_shape(terminate_job, object(), 1, native_name="TerminateJobObject")
        log_sources = tuple(sources)
        terminate = windows_terminate if platform == "win32" else posix_terminate

        async def terminate_process_tree(process: Any) -> None:
            # transport 的有界清理可能先返回；helper 协程自己持有不可变来源及租约，
            # 直到实际 SDK 调用结束，不依赖生命周期 owner 是否仍存活。
            with isolate_sdk_logs(log_sources):
                if platform == "win32" and process in _OWNED_PROCESS_JOBS:
                    # 终止可以使用 handle，但不能释放/丢失关闭责任。
                    terminate_job(_OWNED_PROCESS_JOBS[process], 1)
                await terminate(process)

        def close_process_job(process: Any) -> bool:
            if platform != "win32" or process not in _OWNED_PROCESS_JOBS:
                return True  # 本 adapter 未持有该进程的 Job 资源。
            handle = _OWNED_PROCESS_JOBS[process]
            close_handle(handle)  # 不抑制 pywin32 错误；失败时保留 exact handle。
            del _OWNED_PROCESS_JOBS[process]
            return True

        async def create_process(parameters: Any, errlog: TextIO) -> Any:
            # env 已经过审批；不能叠加 SDK 默认环境，也不重新解析已批准的可执行文件。
            if platform == "win32":
                process = await windows_create(
                    parameters.command,
                    list(parameters.args),
                    env=dict(parameters.env),
                    errlog=errlog,
                    cwd=parameters.cwd,
                )
                # 此处没有 await；只转移这个 exact process 的 handle，其他 SDK 用户不受影响。
                if process in jobs:
                    _OWNED_PROCESS_JOBS[process] = jobs.pop(process)
                return process
            return await open_process(
                [parameters.command, *parameters.args],
                env=dict(parameters.env),
                cwd=parameters.cwd,
                stderr=errlog,
                start_new_session=True,
            )

        def parse_message(line: str) -> Any:
            try:
                return SessionMessage(validate_json(line, by_name=False))
            except Exception as exc:
                # 验证异常只作为内部值；transport 将其归一化，不记录或格式化原始行。
                return exc

        bindings = MCPStdioBindings(
            create_process=create_process,
            terminate_process_tree=terminate_process_tree,
            close_process_job=close_process_job,
            close_subprocess_transport=_close_exact_subprocess_transport,
            create_memory_object_stream=memory_streams,
            parse_message=parse_message,
            closed_resource_errors=closed_errors,
            end_of_stream_errors=(anyio.EndOfStream,),
        )
        return MCPSDK(ClientSession, StdioServerParameters, bindings, log_sources)
    except Exception:
        raise MCPDependencyError(
            "已启用 MCP，但未安装 MCP SDK；请安装项目锁定依赖"
        ) from None
