"""Coding Agent 可调用的本地工具。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any

from tricoder.changes import (
    ChangeBudgetError,
    ChangeJournal,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
)
from tricoder.models import ToolDefinition, ToolResult
from tricoder.patches import PatchError, parse_unified_diff
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy
from tricoder.tools.binding import (
    _DirectoryBinding,
    _PosixDirectoryBinding,
    _WindowsDirectoryBinding,
    _is_windows,
    _stat_identity,
)
from tricoder.tools.command import FinishTool, GitDiffTool, RunCommandTool
from tricoder.tools.filesystem import ListFilesTool, ReadFileTool
from tricoder.tools.gitignore import _GitIgnoreMatcher
from tricoder.tools.handlers import Approver, ToolHandler
from tricoder.tools.search import GlobFilesTool, SearchTextTool
from tricoder.tools.undo import UndoConflictError, UndoExecutor
from tricoder.tools.write import ApplyPatchTool, CreateFileTool, EditFileTool

_HANDLER_CLASSES = (
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    GlobFilesTool,
    EditFileTool,
    CreateFileTool,
    ApplyPatchTool,
    RunCommandTool,
    GitDiffTool,
    FinishTool,
)


@dataclass(slots=True)
class ToolContext:
    """工具执行所需的策略、审批与资源限制。"""

    workspace_policy: WorkspacePolicy
    command_policy: CommandPolicy
    approver: Approver
    read_only: bool = False
    timeout: float = 30.0
    max_output_chars: int = 20_000
    max_search_file_bytes: int = 1_000_000
    change_journal: ChangeJournal | None = None
    # relaxed 级别下对 git 只读命令的自动放行判定（其余命令一律人工审批）。
    auto_approve_git: Callable[[list[str]], bool] | None = None


class ToolRegistry:
    """按固定名称分发工具，统一把预期错误转换为 ToolResult。"""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._handlers: dict[str, ToolHandler] = {
            cls.name: cls(context) for cls in _HANDLER_CLASSES
        }
        self._undo = UndoExecutor(context)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回顺序固定且与内部注册表隔离的公开工具定义。"""
        return tuple(handler.definition() for handler in self._handlers.values())

    def contains(self, name: str) -> bool:
        """判断名称是否在公开注册表中。"""
        return name in self._handlers

    def describe(self, name: str) -> ToolDefinition | None:
        """只返回注册表中静态声明的公开定义。"""
        handler = self._handlers.get(name)
        return None if handler is None else handler.definition()

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(False, f"未知工具：{name}")
        try:
            handler.validate(arguments)
        except (ValueError, TypeError) as exc:
            return ToolResult(False, str(exc))
        try:
            result = handler.run(arguments)
            if name == "apply_patch" and not self.context.read_only:
                audit_paths, change_chars = self._safe_patch_audit_metadata(arguments)
                return replace(
                    result,
                    audit_paths=audit_paths,
                    change_chars=change_chars,
                )
            return result
        except PolicyError as exc:
            result = ToolResult(False, str(exc))
        except ChangeBudgetError as exc:
            result = ToolResult(False, str(exc))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            if name == "apply_patch" and not self.context.read_only:
                result = ToolResult(
                    False,
                    self._safe_patch_filesystem_error(arguments),
                )
            elif isinstance(exc, (OSError, UnicodeError)):
                result = ToolResult(False, "文件系统操作失败")
            else:
                result = ToolResult(False, str(exc))
        if name == "apply_patch" and not self.context.read_only:
            audit_paths, change_chars = self._safe_patch_audit_metadata(arguments)
            return replace(
                result,
                audit_paths=audit_paths,
                change_chars=change_chars,
            )
        return result

    def preview_undo(self, change_set: TaskChangeSet) -> UndoPreview:
        """首次全量核验后预览反向差异，不落盘。"""
        return self._undo.preview_undo(change_set)

    def undo_change_set(self, change_set: TaskChangeSet) -> UndoExecution:
        """全量核验后撤销最近任务，失败时反向补偿。"""
        return self._undo.undo_change_set(change_set)

    @staticmethod
    def _safe_patch_filesystem_error(arguments: dict[str, Any]) -> str:
        """仅从纯解析结果公开规范相对路径，不回显底层异常文本。"""

        paths, _change_chars = ToolRegistry._safe_patch_audit_metadata(arguments)
        if paths:
            return f"补丁文件系统操作失败：{'、'.join(paths)}"
        return "补丁文件系统操作失败"

    @staticmethod
    def _safe_patch_audit_metadata(
        arguments: dict[str, Any],
    ) -> tuple[tuple[str, ...], int]:
        """只用纯解析结果生成补丁审计路径与字符计数。"""

        source = arguments.get("patch")
        if not isinstance(source, str):
            return (), 0
        try:
            file_patches = parse_unified_diff(source)
        except (PatchError, ValueError, TypeError, UnicodeError):
            return (), 0
        paths = tuple(sorted({file_patch.path for file_patch in file_patches}))
        change_chars = sum(
            len(line[1:])
            for file_patch in file_patches
            for hunk in file_patch.hunks
            for line in hunk.lines
            if line.startswith(("+", "-"))
        )
        return paths, change_chars
