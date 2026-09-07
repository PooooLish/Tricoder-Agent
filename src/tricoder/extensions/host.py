"""扩展生命周期宿主；不负责 UI、会话持久化或权限决策。"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Iterable

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.extensions.models import (
    ExtensionDescriptor,
    ExtensionFailure,
    ExtensionKind,
    ExtensionProvider,
    ToolOrigin,
)
from tricoder.tools import ToolRegistry
from tricoder.tools.handlers import ToolHandler


_TOOL_ORIGIN_KINDS = {
    ExtensionKind.MCP: "mcp",
    ExtensionKind.HOOK: "hook",
}
_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ExtensionHost:
    """顺序启动扩展、逆序停止，并把单个扩展故障隔离为安全记录。"""

    def __init__(self, providers: Iterable[ExtensionProvider]) -> None:
        self._providers = tuple(providers)
        if len(self._providers) > 64 or not all(
            isinstance(getattr(provider, "descriptor", None), ExtensionDescriptor)
            for provider in self._providers
        ):
            raise ValueError("扩展 Provider 必须提供有效 descriptor，且总数不超过 64")
        self._started: list[ExtensionProvider] = []
        self._stop_pending: list[ExtensionProvider] = []
        self._failures: list[ExtensionFailure] = []
        self._tool_cache: tuple[tuple[ExtensionProvider, ToolHandler], ...] | None = None

    @property
    def failures(self) -> tuple[ExtensionFailure, ...]:
        return tuple(self._failures)

    def discover(self) -> tuple[ExtensionDescriptor, ...]:
        """按声明顺序返回只含安全字段的 descriptor。"""

        return tuple(provider.descriptor for provider in self._providers)

    async def start(self, cancellation: CancellationToken | None = None) -> None:
        """启动全部已启用扩展；普通失败隔离，取消则回滚已启动资源。"""

        if self._started:
            return
        if self._stop_pending:
            raise RuntimeError("仍有扩展资源等待安全清理")
        # 只有从“无 started、无待清理资源”开始的新一轮启动才失效缓存；
        # 正常重复 start 仍直接返回，避免工具集合和生命周期无谓漂移。
        self._tool_cache = None
        token = cancellation or CancellationToken()
        duplicate_ids = {
            extension_id
            for extension_id, count in Counter(
                provider.descriptor.id for provider in self._providers
            ).items()
            if count > 1
        }
        try:
            for provider in self._providers:
                token.raise_if_cancelled()
                descriptor = provider.descriptor
                if descriptor.id in duplicate_ids:
                    self._record_failure(
                        descriptor.id,
                        "discover",
                        "扩展 id 冲突，已拒绝启动",
                    )
                    continue
                if not descriptor.enabled:
                    continue
                try:
                    await provider.start(token)
                    token.raise_if_cancelled()
                    self._started.append(provider)
                except (CancellationError, asyncio.CancelledError):
                    # 当前 provider 可能在抛出取消前已分配资源。Host 必须和
                    # 普通 start 失败一样取得清理所有权，失败时放入 pending。
                    await self._cleanup_failed_start(provider)
                    raise
                except Exception:
                    self._record_failure(
                        descriptor.id,
                        "start",
                        "扩展启动失败，已隔离",
                    )
                    await self._cleanup_failed_start(provider)
        except (CancellationError, asyncio.CancelledError):
            # 当前 provider 的首次清理失败后已经进入 _stop_pending；本轮只回滚此前
            # 成功启动的 provider，避免立刻对同一失败资源做一次无意义的重复清理。
            await self._stop_started()
            raise

    async def stop(self) -> None:
        """逆序关闭已启动扩展；重复调用没有副作用。"""

        providers = tuple(reversed(self._started)) + tuple(self._stop_pending)
        self._started.clear()
        self._stop_pending.clear()
        self._tool_cache = None
        await self._stop_providers(providers)

    async def _stop_started(self) -> None:
        """仅逆序回滚本轮已成功启动的 Provider，保留既有待清理队列。"""

        providers = tuple(reversed(self._started))
        self._started.clear()
        self._tool_cache = None
        await self._stop_providers(providers)

    async def _stop_providers(
        self,
        providers: tuple[ExtensionProvider, ...],
    ) -> None:
        """逐个停止指定 Provider；失败项进入统一的可重试清理队列。"""

        for provider in providers:
            try:
                await provider.stop()
            except Exception:
                self._stop_pending.append(provider)
                self._record_failure(
                    provider.descriptor.id,
                    "stop",
                    "扩展停止失败，已继续清理其他扩展",
                )

    async def _cleanup_failed_start(self, provider: ExtensionProvider) -> None:
        """清理可能已部分分配资源的 Provider，失败时留待统一 stop 重试。"""

        try:
            await provider.stop()
        except Exception:
            self._stop_pending.append(provider)
            self._record_failure(
                provider.descriptor.id,
                "stop",
                "扩展停止失败，已继续清理其他扩展",
            )

    def tool_handlers(self) -> tuple[ToolHandler, ...]:
        """返回无扩展间名称冲突的处理器；冲突双方都不会获胜。"""

        return tuple(handler for _provider, handler in self._resolved_tools())

    def prompt_fragments(self) -> tuple[str, ...]:
        """聚合已启动扩展的有界非空 prompt 片段，单项失败不影响其他扩展。"""

        fragments: list[str] = []
        for provider in tuple(self._started):
            try:
                provided = provider.prompt_fragments()
                if not isinstance(provided, tuple) or not all(
                    isinstance(fragment, str)
                    and bool(fragment.strip())
                    and len(fragment) <= 10_000
                    for fragment in provided
                ):
                    raise ValueError("invalid prompt fragments")
                fragments.extend(provided)
            except Exception:
                self._record_failure(
                    provider.descriptor.id,
                    "prompt",
                    "扩展 prompt 无效，已隔离",
                )
        return tuple(fragments)

    def register_tools(self, registry: ToolRegistry) -> int:
        """把已解析动态工具注册到统一网关；内置冲突保持内置优先。"""

        return len(self.register_tool_handlers(registry))

    def register_tool_handlers(
        self,
        registry: ToolRegistry,
    ) -> tuple[ToolHandler, ...]:
        """原子注册本轮工具；异常时只按身份回滚本轮已成功的前缀。"""

        registered: list[ToolHandler] = []
        try:
            for provider, handler in self._resolved_tools():
                descriptor = provider.descriptor
                kind = _TOOL_ORIGIN_KINDS.get(descriptor.kind)
                risk = getattr(handler, "risk", None)
                try:
                    if kind is None or not isinstance(risk, str):
                        raise ValueError("扩展工具缺少来源或风险声明")
                    registry.register(
                        handler,
                        origin=ToolOrigin(kind, descriptor.id, risk),
                    )
                except (TypeError, ValueError):
                    self._record_failure(
                        descriptor.id,
                        "tool_conflict",
                        "扩展工具注册失败或与内置工具冲突",
                        getattr(handler, "name", None),
                    )
                    continue
                registered.append(handler)
        except BaseException:
            # 回滚故障不能替换触发事务失败的原异常或取消对象。
            for handler in reversed(registered):
                try:
                    registry.unregister(handler)
                except BaseException:
                    pass
            raise
        return tuple(registered)

    def _resolved_tools(self) -> tuple[tuple[ExtensionProvider, ToolHandler], ...]:
        if self._tool_cache is not None:
            return self._tool_cache
        candidates: list[tuple[ExtensionProvider, ToolHandler]] = []
        for provider in tuple(self._started):
            try:
                handlers = provider.tool_handlers()
                if not isinstance(handlers, tuple) or not all(
                    isinstance(handler, ToolHandler) for handler in handlers
                ):
                    raise ValueError("invalid handlers")
                valid_handlers = tuple(
                    handler
                    for handler in handlers
                    if isinstance(getattr(handler, "name", None), str)
                    and _TOOL_NAME_PATTERN.fullmatch(handler.name)
                )
                if len(valid_handlers) != len(handlers):
                    self._record_failure(
                        provider.descriptor.id,
                        "tools",
                        "扩展工具名称无效，已隔离",
                    )
                candidates.extend((provider, handler) for handler in valid_handlers)
            except Exception:
                self._record_failure(
                    provider.descriptor.id,
                    "tools",
                    "扩展工具列表无效，已隔离",
                )
        counts = Counter(handler.name for _provider, handler in candidates)
        resolved: list[tuple[ExtensionProvider, ToolHandler]] = []
        for provider, handler in candidates:
            if counts[handler.name] > 1:
                self._record_failure(
                    provider.descriptor.id,
                    "tool_conflict",
                    "扩展工具名称冲突，冲突方均未注册",
                    handler.name,
                )
            else:
                resolved.append((provider, handler))
        self._tool_cache = tuple(resolved)
        return self._tool_cache

    def _record_failure(
        self,
        extension_id: str,
        phase: str,
        safe_message: str,
        tool_name: str | None = None,
    ) -> None:
        failure = ExtensionFailure(extension_id, phase, safe_message, tool_name)
        if failure not in self._failures:
            self._failures.append(failure)
