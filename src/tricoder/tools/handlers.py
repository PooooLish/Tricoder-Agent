"""工具处理器的共享基类：策略上下文、快照与变更账本交互。"""

from __future__ import annotations

import copy
import stat
from typing import TYPE_CHECKING, Any, Callable

from tricoder.changes import (
    ChangeBudgetError,
    FileChange,
    FileSnapshot,
)
from tricoder.models import ToolDefinition, ToolResult

from tricoder.tools.binding import _DirectoryBinding

if TYPE_CHECKING:
    from tricoder.tools import ToolContext


Approver = Callable[[str, str], bool]


class ToolHandler:
    """工具处理器的公共能力；子类声明定义并实现 ``run``。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def __init__(self, context: "ToolContext") -> None:
        self.context = context

    def definition(self) -> ToolDefinition:
        """返回每次独立深拷贝的工具定义，隔离注册表与调用方。"""
        return ToolDefinition(
            self.name,
            self.description,
            copy.deepcopy(self.parameters),
        )

    def validate(self, arguments: object) -> None:
        """在进入处理器前校验当前工具 Schema 支持的基础类型。"""
        self._validate_arguments(self.parameters, arguments)

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError

    @staticmethod
    def _schema(
        properties: dict[str, dict[str, str]],
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        """构建当前简单工具所需的统一对象 Schema。"""
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: object) -> None:
        """在进入处理器前校验当前工具 Schema 支持的基础类型。"""
        if schema.get("type") != "object" or not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")

        properties = schema["properties"]
        for name in schema["required"]:
            if name not in arguments:
                raise ValueError(f"缺少必填参数：{name}")
        if not schema["additionalProperties"]:
            extras = set(arguments) - set(properties)
            if extras:
                raise ValueError(f"不支持额外参数：{sorted(extras)[0]}")

        validators: dict[str, type[object]] = {
            "string": str,
            "integer": int,
            "boolean": bool,
        }
        for name, value in arguments.items():
            expected = properties[name]["type"]
            expected_type = validators.get(expected)
            if expected_type is None:
                raise ValueError(f"不支持的参数类型：{expected}")
            if not isinstance(value, expected_type) or (
                expected == "integer" and isinstance(value, bool)
            ):
                raise ValueError(f"参数 {name} 必须是 {expected}")

    @staticmethod
    def _snapshot(
        binding: _DirectoryBinding,
        name: str,
        relative: str,
    ) -> FileSnapshot:
        """通过已绑定目录读取 UTF-8 内容、规范权限与真实文件身份。"""

        content, identity, mode = binding.read_text(name)
        return FileSnapshot(relative, content, stat.S_IMODE(mode), identity)

    def _reserve_change(self, change: FileChange) -> None:
        """有活动账本时在审批前预留字符预算。"""

        if self.context.change_journal is not None:
            self.context.change_journal.reserve((change,))

    def _reserve_changes(self, changes: tuple[FileChange, ...]) -> None:
        """有活动账本时为一次多文件审批整体预留字符预算。"""

        if self.context.change_journal is not None:
            self.context.change_journal.reserve(changes)

    def _record_committed(
        self,
        path: str,
        before: FileSnapshot | None,
        after: FileSnapshot | None,
    ) -> None:
        """仅在文件系统提交点之后记录真实快照。"""

        if self.context.change_journal is not None:
            self.context.change_journal.record_committed(path, before, after)

    def _journal_before_is_continuous(
        self,
        path: str,
        before: FileSnapshot | None,
    ) -> bool:
        """由工具边界证明连续调用仍从上一已提交 after 开始。"""

        journal = self.context.change_journal
        if journal is None:
            return True
        if journal.is_tainted(path):
            return False
        has_previous, expected_after = journal.active_after(path)
        if not has_previous or expected_after == before:
            return True
        journal.mark_tainted(path)
        return False

    def _record_unverified_expected(
        self,
        path: str,
        before: FileSnapshot | None,
        expected_after: FileSnapshot | None,
    ) -> None:
        """保存工具预期态并标记不可预览/撤销，绝不读取或记录外部态。"""

        try:
            self._record_committed(path, before, expected_after)
        except Exception:
            pass
        self._mark_journal_tainted(path)

    def _mark_journal_tainted(self, path: str) -> None:
        """尽力标记无法证明归属的路径，且不读取或保存其源码。"""

        journal = self.context.change_journal
        if journal is None:
            return
        try:
            journal.mark_tainted(path)
        except Exception:
            pass

    def _bounded(self, text: str) -> str:
        if len(text) <= self.context.max_output_chars:
            return text
        omitted = len(text) - self.context.max_output_chars
        return f"{text[: self.context.max_output_chars]}\n...（已截断 {omitted} 个字符）"

    @staticmethod
    def _required_str(
        arguments: dict[str, Any],
        name: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ValueError(f"{name} 必须是字符串且不能为空")
        return value
