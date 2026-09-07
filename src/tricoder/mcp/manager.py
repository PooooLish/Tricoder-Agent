"""任务级 MCP 多 server 管理、ExtensionHost 适配与精确调用路由。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from tricoder.audit import AuditLogger
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.extensions.host import ExtensionHost
from tricoder.extensions.models import (
    ExtensionDescriptor,
    ExtensionFailure,
    ExtensionKind,
    ExtensionTrust,
)
from tricoder.models import AppConfig, MCPServerConfig
from tricoder.tools import ToolContext, ToolRegistry

from .client import MCPClient, MCPClientError, MCPCleanupError
from .models import MCPCallResult, MCPServerState, MCPToolSpec
from .security import MCPLaunchRequest, approve_mcp_start, prepare_mcp_launch
from .tool_adapter import MCPToolHandler


class MCPManagerError(RuntimeError):
    """manager 对外使用的固定、无底层正文错误类别。"""


class _MCPClientProtocol(Protocol):
    @property
    def state(self) -> MCPServerState: ...

    async def start(self, cancellation: CancellationToken) -> None: ...

    async def list_tools(
        self,
        cancellation: CancellationToken,
    ) -> tuple[MCPToolSpec, ...]: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult: ...

    async def stop(self) -> None: ...


_ClientFactory = Callable[[str, MCPLaunchRequest], _MCPClientProtocol]


class _MCPServerExtension:
    """把单个已启用 MCP 配置适配为 ExtensionProvider。"""

    def __init__(
        self,
        server: MCPServerConfig,
        manager: "MCPManager",
        context: ToolContext,
        source_env: Mapping[str, str],
        audit: AuditLogger | None,
        client_factory: _ClientFactory,
    ) -> None:
        self.descriptor = ExtensionDescriptor(
            id=server.id,
            kind=ExtensionKind.MCP,
            source=f"mcp/{server.id}",
            enabled=True,
            trust=ExtensionTrust.PROJECT,
        )
        self._server = server
        self._manager = manager
        self._context = context
        self._source_env = source_env
        self._audit = audit
        self._client_factory = client_factory
        self._client: _MCPClientProtocol | None = None
        self._specs: tuple[MCPToolSpec, ...] = ()
        self._handlers: tuple[MCPToolHandler, ...] = ()
        self._ready = False
        self.stop_failed = False
        self.stop_cancelled: asyncio.CancelledError | None = None

    @property
    def specs(self) -> tuple[MCPToolSpec, ...]:
        return self._specs if self._ready else ()

    async def start(self, cancellation: CancellationToken) -> None:
        """审计预写成功后才校验、审批并启动 client。"""

        started = time.perf_counter()
        list_started = started
        failure_phase = "start"
        self._required_start_audit()
        cancellation.raise_if_cancelled()
        try:
            request = prepare_mcp_launch(
                self._server,
                workspace=self._manager.workspace,
                source_env=self._source_env,
            )
            approve_mcp_start(
                request,
                self.descriptor.id,
                self._context.approver,
            )
            cancellation.raise_if_cancelled()
            client = self._client_factory(self.descriptor.id, request)
            self._client = client
            await client.start(cancellation)
            self._manager._audit_lifecycle(
                self.descriptor.id,
                phase="start",
                status="ready",
                started=started,
            )

            failure_phase = "list_tools"
            list_started = time.perf_counter()
            specs = await client.list_tools(cancellation)
            if not isinstance(specs, tuple) or not all(
                isinstance(spec, MCPToolSpec)
                and spec.server_id == self.descriptor.id
                for spec in specs
            ):
                raise MCPManagerError("mcp_tool_list_invalid")
            self._specs = specs
            self._handlers = tuple(
                MCPToolHandler(self._context, self._manager, spec)
                for spec in specs
            )
            self._ready = True
            self._manager._audit_tool_list(
                self.descriptor.id,
                specs,
                started=list_started,
                status="ok",
            )
        except (CancellationError, asyncio.CancelledError):
            # 当前 provider 的清理所有权统一交给 ExtensionHost；这里只记录
            # 固定状态并保留原取消异常，避免 provider 自行清理后脱离 pending。
            self._manager._audit_lifecycle(
                self.descriptor.id,
                phase=failure_phase,
                status="cancelled",
                started=list_started if failure_phase == "list_tools" else started,
            )
            raise
        except Exception:
            self._manager._audit_lifecycle(
                self.descriptor.id,
                phase=failure_phase,
                status="failed",
                started=list_started if failure_phase == "list_tools" else started,
            )
            raise

    async def stop(self) -> None:
        """关闭 client；审计写失败不能阻止实际清理或其他 server。"""

        client = self._client
        self._ready = False
        self._specs = ()
        self._handlers = ()
        if client is None:
            self.stop_failed = False
            return
        started = time.perf_counter()
        self._manager._audit_lifecycle(
            self.descriptor.id,
            phase="stop",
            status="begin",
            started=started,
        )
        try:
            await client.stop()
        except asyncio.CancelledError as cancellation:
            # MCPClient 的公开契约会在完成受 shield 保护的自身清理后重新抛出
            # 调用方取消。这里延迟传播，让 Host 继续关闭其余 server。
            self.stop_cancelled = cancellation
            if client.state is not MCPServerState.STOPPED:
                # 取消不能证明清理成功；交由 Host 保留本 provider 并继续关闭其他项。
                self.stop_failed = True
                self._manager._audit_lifecycle(
                    self.descriptor.id,
                    phase="stop",
                    status="failed",
                    started=started,
                )
                raise MCPCleanupError("mcp_cleanup_failed") from None
            self.stop_failed = False
            self._client = None
            self._manager._audit_lifecycle(
                self.descriptor.id,
                phase="stop",
                status="cancelled",
                started=started,
            )
            return
        except Exception:
            self.stop_failed = True
            self._manager._audit_lifecycle(
                self.descriptor.id,
                phase="stop",
                status="failed",
                started=started,
            )
            raise
        self.stop_failed = False
        self.stop_cancelled = None
        self._client = None
        self._manager._audit_lifecycle(
            self.descriptor.id,
            phase="stop",
            status="stopped",
            started=started,
        )

    def tool_handlers(self) -> tuple[MCPToolHandler, ...]:
        return self._handlers if self._ready else ()

    def prompt_fragments(self) -> tuple[str, ...]:
        return ()

    async def call_tool(
        self,
        raw_name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult:
        client = self._client
        if not self._ready or client is None:
            raise MCPManagerError("mcp_server_unavailable")
        return await client.call_tool(raw_name, arguments, cancellation)

    def _required_start_audit(self) -> None:
        if self._audit is None:
            return
        # 首条记录是启动执行的 fail-closed 证据；写入失败时 client 尚未创建。
        self._audit.log(
            {
                "event": "mcp_lifecycle",
                "server_id": self.descriptor.id,
                "phase": "start",
                "status": "begin",
                "duration_ms": 0,
            }
        )


class MCPManager:
    """在一个 Coding Task 内拥有全新的 MCP providers、Host 与路由表。"""

    def __init__(
        self,
        config: AppConfig,
        context: ToolContext,
        source_env: Mapping[str, str],
        audit: AuditLogger | None,
        *,
        client_factory: _ClientFactory = MCPClient,
    ) -> None:
        if not isinstance(config, AppConfig):
            raise TypeError("config 必须是 AppConfig")
        if not isinstance(context, ToolContext):
            raise TypeError("context 必须是 ToolContext")
        if not isinstance(source_env, Mapping):
            raise TypeError("source_env 必须是环境映射")
        if not callable(client_factory):
            raise TypeError("client_factory 必须可调用")
        self.workspace = config.workspace
        self._audit = audit
        self._source_env = dict(source_env)
        servers = (
            tuple(server for server in config.mcp.servers if server.enabled)
            if config.extensions.enabled and config.mcp.enabled
            else ()
        )
        self._extensions = tuple(
            _MCPServerExtension(
                server,
                self,
                context,
                self._source_env,
                audit,
                client_factory,
            )
            for server in servers
        )
        self._host = ExtensionHost(self._extensions)
        self._routes: dict[tuple[str, str], _MCPServerExtension] = {}
        self._active = False
        self._start_attempted = False
        self._registration_complete = False
        self._registered_handlers: tuple[MCPToolHandler, ...] = ()

    @property
    def descriptors(self) -> tuple[ExtensionDescriptor, ...]:
        return self._host.discover()

    @property
    def failures(self) -> tuple[ExtensionFailure, ...]:
        return self._host.failures

    async def start_all(self, cancellation: CancellationToken) -> None:
        """按配置顺序启动；普通失败隔离，取消由 Host 反向回滚后传播。"""

        cancellation.raise_if_cancelled()
        if self._start_attempted:
            return
        self._start_attempted = True
        try:
            await self._host.start(cancellation)
        except (CancellationError, asyncio.CancelledError):
            self._active = False
            self._routes.clear()
            raise
        self._active = True

    def register_tools(self, registry: ToolRegistry) -> int:
        """注册无冲突工具，并只为真正注册成功的 exact 名称建立路由。"""

        if not self._active or self._registration_complete:
            return 0
        expected_handlers = tuple(
            handler
            for extension in self._extensions
            for handler in extension.tool_handlers()
        )
        preexisting_ids = {
            id(handler)
            for handler in expected_handlers
            if registry.is_registered(handler)
        }
        self._host.register_tool_handlers(registry)
        # 注册凭据来自 manager 自己创建的 handler 身份与 registry 的前后状态，
        # 不信任 Host 返回值中的重复、遗漏或外来对象。
        registered_handlers = tuple(
            handler
            for handler in expected_handlers
            if isinstance(handler, MCPToolHandler)
            and id(handler) not in preexisting_ids
            and registry.is_registered(handler)
        )
        self._registered_handlers = registered_handlers
        routes: dict[tuple[str, str], _MCPServerExtension] = {}
        for extension in self._extensions:
            for spec, handler in zip(
                extension.specs,
                extension.tool_handlers(),
                strict=True,
            ):
                if any(
                    handler is registered_handler
                    for registered_handler in registered_handlers
                ):
                    routes[(extension.descriptor.id, spec.raw_name)] = extension
        self._routes = routes
        self._registration_complete = True
        return len(registered_handlers)

    def unregister_tools(self, registry: ToolRegistry) -> int:
        """按 handler 身份撤销且只撤销本 manager 实际注册的工具。"""

        handlers = self._registered_handlers
        self._registered_handlers = ()
        self._routes.clear()
        return sum(1 for handler in handlers if registry.unregister(handler))

    async def call_tool(
        self,
        server_id: str,
        raw_name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult:
        """仅允许已注册的 exact server/raw-name 路由，并复制参数快照。"""

        cancellation.raise_if_cancelled()
        if not self._active or not any(
            extension.descriptor.id == server_id and extension.specs
            for extension in self._extensions
        ):
            raise MCPManagerError("mcp_server_unavailable")
        extension = self._routes.get((server_id, raw_name))
        if extension is None:
            raise MCPManagerError("mcp_tool_unknown")
        if not isinstance(arguments, dict) or not all(
            isinstance(name, str) for name in arguments
        ):
            raise MCPManagerError("mcp_arguments_invalid")
        try:
            copied_arguments = copy.deepcopy(arguments)
        except Exception:
            raise MCPManagerError("mcp_arguments_invalid") from None

        started = time.perf_counter()
        self._audit_tool_call(
            server_id,
            raw_name,
            argument_keys=tuple(sorted(arguments)),
            status="begin",
            started=started,
        )
        try:
            result = await extension.call_tool(
                raw_name,
                copied_arguments,
                cancellation,
            )
        except CancellationError:
            raise
        except (MCPClientError, MCPManagerError):
            self._audit_tool_call(
                server_id,
                raw_name,
                argument_keys=tuple(sorted(arguments)),
                status="failed",
                started=started,
            )
            raise
        except Exception:
            self._audit_tool_call(
                server_id,
                raw_name,
                argument_keys=tuple(sorted(arguments)),
                status="failed",
                started=started,
            )
            raise MCPManagerError("mcp_tool_failed") from None
        if not isinstance(result, MCPCallResult):
            raise MCPManagerError("mcp_tool_failed")
        self._audit_tool_call(
            server_id,
            raw_name,
            argument_keys=tuple(sorted(arguments)),
            status="ok" if result.ok else "tool_error",
            started=started,
            output_chars=len(result.text),
            omitted_count=len(result.omitted_content_types),
        )
        return result

    async def stop_all(self) -> None:
        """停止接受调用并让 Host 逆序清理；成功后重复调用无副作用。"""

        self._active = False
        self._routes.clear()
        await self._host.stop()
        stop_cancelled = next(
            (extension.stop_cancelled for extension in reversed(self._extensions)
             if extension.stop_cancelled is not None),
            None,
        )
        for extension in self._extensions:
            extension.stop_cancelled = None
        if stop_cancelled is not None:
            raise stop_cancelled
        if any(extension.stop_failed for extension in self._extensions):
            raise MCPCleanupError("mcp_cleanup_failed")

    def _audit_lifecycle(
        self,
        server_id: str,
        *,
        phase: str,
        status: str,
        started: float,
    ) -> None:
        self._log_best_effort(
            {
                "event": "mcp_lifecycle",
                "server_id": server_id,
                "phase": phase,
                "status": status,
                "duration_ms": _elapsed_ms(started),
            }
        )

    def _audit_tool_list(
        self,
        server_id: str,
        specs: tuple[MCPToolSpec, ...],
        *,
        started: float,
        status: str,
    ) -> None:
        raw_names = tuple(sorted(spec.raw_name for spec in specs))
        digest = hashlib.sha256("\0".join(raw_names).encode("utf-8")).hexdigest()
        self._log_best_effort(
            {
                "event": "mcp_lifecycle",
                "server_id": server_id,
                "phase": "list_tools",
                "status": status,
                "duration_ms": _elapsed_ms(started),
                "tool_name_digest": digest,
                "tool_name_chars": sum(len(name) for name in raw_names),
                "tool_count": len(raw_names),
            }
        )

    def _audit_tool_call(
        self,
        server_id: str,
        raw_name: str,
        *,
        argument_keys: tuple[str, ...],
        status: str,
        started: float,
        output_chars: int | None = None,
        omitted_count: int | None = None,
    ) -> None:
        event: dict[str, object] = {
            "event": "mcp_tool",
            "server_id": server_id,
            "phase": "call_tool",
            "status": status,
            "duration_ms": _elapsed_ms(started),
            "origin": {"kind": "mcp", "id": server_id, "risk": "dangerous"},
            "tool_name_digest": hashlib.sha256(raw_name.encode("utf-8")).hexdigest(),
            "tool_name_chars": len(raw_name),
            "argument_keys": list(argument_keys),
            "argument_count": len(argument_keys),
        }
        if output_chars is not None:
            event["output_chars"] = output_chars
        if omitted_count is not None:
            event["omitted_count"] = omitted_count
        self._log_best_effort(event)

    def _log_best_effort(self, event: dict[str, object]) -> None:
        if self._audit is None:
            return
        try:
            self._audit.log(event)
        except Exception:
            # 首条 start/begin 在 provider 内单独 fail-closed；此后的审计
            # 故障不得阻止 server 关闭或其他 server 的清理。
            return


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
