"""TriCoder 扩展描述、生命周期宿主与工具来源公共接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tricoder.extensions.models import (
    ExtensionDescriptor,
    ExtensionFailure,
    ExtensionKind,
    ExtensionProvider,
    ExtensionTrust,
    ToolOrigin,
)

if TYPE_CHECKING:
    from tricoder.extensions.host import ExtensionHost

__all__ = [
    "ExtensionDescriptor",
    "ExtensionFailure",
    "ExtensionHost",
    "ExtensionKind",
    "ExtensionProvider",
    "ExtensionTrust",
    "ToolOrigin",
]


def __getattr__(name: str) -> object:
    """惰性导出 Host，避免 ToolRegistry 读取纯模型时形成导入环。"""

    if name == "ExtensionHost":
        from tricoder.extensions.host import ExtensionHost

        return ExtensionHost
    raise AttributeError(name)
