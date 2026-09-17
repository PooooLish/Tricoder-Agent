"""单个 Coding Task 的 MCP 异步作用域。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TypeVar

from tricoder.audit import AuditLogger
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.task_cleanup import TaskCleanup, cleanup_scope, current_cleanup
from tricoder.models import AppConfig, RunResult, SessionTurnResult, ToolResult
from tricoder.execution_state import ErrorCode
from tricoder.tools import ToolContext, ToolRegistry

from .client import MCPCleanupError
from .manager import MCPManager
from .sdk import load_mcp_sdk


T = TypeVar("T")
ManagerFactory = Callable[
    [AppConfig, ToolContext, Mapping[str, str], AuditLogger | None],
    object,
]


def _audit_cleanup_failure(audit: AuditLogger | None) -> None:
    """尽力记录固定分类；审计故障不得覆盖任务的主异常。"""

    if audit is None:
        return
    try:
        audit.log(
            {
                "event": "mcp_lifecycle",
                "phase": "task_cleanup",
                "status": "failed",
                "error": "mcp_cleanup_failed",
            }
        )
    except Exception:
        pass


async def run_mcp_task(
    config: AppConfig,
    registry: ToolRegistry,
    *,
    source_env: Mapping[str, str],
    audit: AuditLogger | None,
    cancellation: CancellationToken,
    operation: Callable[[ToolRegistry], Awaitable[T]],
    manager_factory: ManagerFactory = MCPManager,
) -> T:
    """在同一事件循环内启动、注册、执行并始终回收 MCP manager。"""

    parent_scope = current_cleanup()
    scope = parent_scope or TaskCleanup()
    with cleanup_scope(scope):
        try:
            result = await _run_mcp_task_owned(
                config, registry, source_env=source_env, audit=audit,
                cancellation=cancellation, operation=operation, manager_factory=manager_factory,
            )
            if scope.has_pending:
                scope.mark_failed()
            if scope.failed:
                # 只降级明确的本地结果类型，保留 effects/context；泛型成功不能被猜测解释。
                if type(result) is RunResult:
                    return replace(result, cleanup_failed=True)
                if type(result) is SessionTurnResult and type(result.result) is RunResult:
                    return replace(result, result=replace(result.result, cleanup_failed=True))
                if (type(result) is ToolResult and not result.ok and result.error is not None
                        and result.error.code is ErrorCode.CLEANUP_FAILED):
                    return result
                raise MCPCleanupError(cleanup_owner=scope)
            return result
        except CancellationError as primary:
            if scope.failed or scope.has_pending:
                scope.mark_failed()
                primary.record_cleanup_failure(scope)
            raise
        finally:
            # 所有异常保留首对象与链；所有权由宿主交接，不给 foreign 异常动态打属性。
            if scope.has_pending:
                scope.mark_failed()
            if parent_scope is None:
                # 独立入口的 registry 比本次调用活得久；成功/失败出口都移交资源。
                registry.retain_cleanup(scope)


async def _run_mcp_task_owned(
    config: AppConfig,
    registry: ToolRegistry,
    *,
    source_env: Mapping[str, str],
    audit: AuditLogger | None,
    cancellation: CancellationToken,
    operation: Callable[[ToolRegistry], Awaitable[T]],
    manager_factory: ManagerFactory,
) -> T:

    # Host 会隔离单 server 的普通启动失败；SDK 缺失属于整体配置错误，
    # 必须在默认 manager 创建前单独预检，不能退化成“零 MCP 工具成功”。
    if manager_factory is MCPManager:
        load_mcp_sdk()
    manager = manager_factory(config, registry.context, source_env, audit)
    primary_error: BaseException | None = None
    try:
        await manager.start_all(cancellation)  # type: ignore[attr-defined]
        manager.register_tools(registry)  # type: ignore[attr-defined]
        return await operation(registry)
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            # 同步 Runner 的中断先通知共享令牌，再进入下面统一清理出口。
            cancellation.cancel()
        primary_error = exc
        raise
    finally:
        scope = current_cleanup()
        if scope is not None:
            scope.begin_termination()
        cleanup_error: BaseException | None = None
        try:
            manager.unregister_tools(registry)  # type: ignore[attr-defined]
        except BaseException as exc:
            cleanup_error = exc
        try:
            await manager.stop_all()  # type: ignore[attr-defined]
        except BaseException as exc:
            # cleanup 的显式任务取消优先于普通清理错误；二者都不能覆盖主异常。
            if cleanup_error is None or isinstance(exc, asyncio.CancelledError):
                cleanup_error = exc
        if cleanup_error is not None:
            scope = current_cleanup()
            if scope is not None:
                scope.mark_failed()
            _audit_cleanup_failure(audit)
            if primary_error is None:
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                # 成功路径无法确认资源已经回收时，必须改为稳定失败。
                raise MCPCleanupError("mcp_cleanup_failed") from None


def run_mcp_task_sync(
    config: AppConfig,
    registry: ToolRegistry,
    *,
    source_env: Mapping[str, str],
    audit: AuditLogger | None,
    cancellation: CancellationToken,
    operation: Callable[[ToolRegistry], Awaitable[T]],
    manager_factory: ManagerFactory = MCPManager,
) -> T:
    """为同步调用方运行完整作用域；已有事件循环时明确拒绝。"""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("同步 MCP 入口不能在已运行的事件循环中调用")
    return asyncio.run(
        run_mcp_task(
            config,
            registry,
            source_env=source_env,
            audit=audit,
            cancellation=cancellation,
            operation=operation,
            manager_factory=manager_factory,
        )
    )
