"""不依赖第三方 SDK 的 MCP 内部模型。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from enum import Enum


MAX_DESCRIPTION_CHARS = 2_000
MAX_RESULT_CHARS = 200_000
MAX_RAW_NAME_CHARS = 1_024
MAX_SERVER_ID_CHARS = 64
MAX_PUBLIC_NAME_CHARS = 64

_PUBLIC_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class MCPServerState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True, init=False)
class MCPToolSpec:
    server_id: str
    raw_name: str
    public_name: str
    description: str
    _input_schema: dict[str, object] = field(repr=False)

    def __init__(
        self,
        server_id: str,
        raw_name: str,
        public_name: str,
        description: str,
        input_schema: dict[str, object],
    ) -> None:
        _require_bounded_text("server_id", server_id, MAX_SERVER_ID_CHARS)
        _require_bounded_text("raw_name", raw_name, MAX_RAW_NAME_CHARS)
        if not isinstance(public_name, str) or not _PUBLIC_NAME_PATTERN.fullmatch(
            public_name
        ):
            raise ValueError("public_name 必须是最长 64 字符的规范化标识")
        if not isinstance(description, str):
            raise TypeError("description 必须是字符串")
        if not isinstance(input_schema, dict):
            raise TypeError("input_schema 必须是字典")
        object.__setattr__(self, "server_id", server_id)
        object.__setattr__(self, "raw_name", raw_name)
        object.__setattr__(self, "public_name", public_name)
        object.__setattr__(self, "description", description[:MAX_DESCRIPTION_CHARS])
        object.__setattr__(self, "_input_schema", copy.deepcopy(input_schema))

    @property
    def input_schema(self) -> dict[str, object]:
        """每次返回独立深拷贝，避免调用者改写内部工具契约。"""

        return copy.deepcopy(self._input_schema)


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    ok: bool
    text: str
    omitted_content_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise TypeError("ok 必须是 boolean")
        if not isinstance(self.text, str):
            raise TypeError("text 必须是字符串")
        if len(self.text) > MAX_RESULT_CHARS:
            raise ValueError(f"text 超过 {MAX_RESULT_CHARS} 字符上限")
        if not isinstance(self.omitted_content_types, tuple) or not all(
            isinstance(item, str) for item in self.omitted_content_types
        ):
            raise TypeError("omitted_content_types 必须是字符串元组")


def _require_bounded_text(name: str, value: object, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是字符串")
    if not value or len(value) > maximum:
        raise ValueError(f"{name} 必须是 1 到 {maximum} 个字符")
