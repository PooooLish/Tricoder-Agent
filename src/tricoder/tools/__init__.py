"""Coding Agent 可调用的本地工具。"""

from __future__ import annotations

import copy
import re
import secrets
import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from tricoder.core.cancellation import CancellationError, CancellationToken, cancellation_scope
from tricoder.task_cleanup import TaskCleanup, current_cleanup, task_cleanup_scope
from tricoder.context.spill import SpillError, ToolResultSpillStore
from tricoder.extensions.models import ToolOrigin
from tricoder.execution_state import EffectState, ErrorCode, FileEffects, RecoveryAction, ToolError
from tricoder.mcp.schema import validate_mcp_schema
from tricoder.changes import (
    ChangeBudgetError,
    ChangeJournal,
    TaskChangeSet,
    UndoExecution,
    UndoPreview,
)
from tricoder.models import ToolDefinition, ToolResult, tool_failure
from tricoder.patches import PatchError, parse_unified_diff
from tricoder.policy import CommandPolicy, PolicyArgumentError, PolicyError, WorkspacePolicy
from tricoder.verification import VerificationScope
from tricoder.task_observation import current_task_observation
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
from tricoder.tools.handlers import Approver, InvalidToolArgument, ToolHandler
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

_READ_TOOL_RESULT_DEFINITION = ToolDefinition(
    "read_tool_result",
    "按不可猜引用分段读取当前 Session 的大型工具结果。",
    {
        "type": "object",
        "properties": {
            "reference": {"type": "string"},
            "offset": {"type": "integer"},
        },
        "required": ["reference"],
        "additionalProperties": False,
    },
)

_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BUILTIN_RISKS = {
    "list_files": "read",
    "read_file": "read",
    "search_text": "read",
    "glob_files": "read",
    "edit_file": "write",
    "create_file": "write",
    "apply_patch": "write",
    "run_command": "process",
    "git_diff": "read",
    "finish": "read",
    "read_tool_result": "read",
}


def _mcp_handler_class() -> type[ToolHandler]:
    """仅在执行 MCP 工具时加载适配器，避免 MCP 与 tools 冷启动循环导入。"""

    from tricoder.mcp.tool_adapter import MCPToolHandler

    return MCPToolHandler


def __getattr__(name: str):
    """按需保留旧的 MCPToolHandler 包级导出，不恢复顶层循环依赖。"""

    if name == "MCPToolHandler":
        handler_class = _mcp_handler_class()
        # 缓存真实类，确保后续属性访问和直接适配器导入保持对象身份一致。
        globals()[name] = handler_class
        return handler_class
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _InvalidToolResultError(TypeError):
    """处理器违反公开 ToolResult 返回契约。"""


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
    # 大型结果只写入 Session 绑定的 TriCoder 运行目录，不写目标源码目录。
    spill_store: ToolResultSpillStore | None = None
    verification_scope: VerificationScope = field(default_factory=VerificationScope)


class ToolRegistry:
    """按固定名称分发工具，统一把预期错误转换为 ToolResult。"""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        # 跨任务只保留 pending 资源；它不是任何任务/操作的 deadline scope。
        self._standalone_cleanup = TaskCleanup()
        # 注册与按身份撤销必须在同一原子边界内完成。
        self._registration_lock = threading.RLock()
        self._handlers: dict[str, ToolHandler] = {}
        self._origins: dict[str, ToolOrigin] = {}
        self._definitions: dict[str, ToolDefinition] = {}
        self._builtin_handlers: dict[str, ToolHandler] = {}
        for handler_class in _HANDLER_CLASSES:
            handler = handler_class(context)
            self.register(
                handler,
                origin=ToolOrigin("builtin", "tricoder", _BUILTIN_RISKS[handler.name]),
            )
            self._builtin_handlers[handler.name] = handler
        if self.context.spill_store is not None:
            self._origins["read_tool_result"] = ToolOrigin(
                "builtin", "tricoder", _BUILTIN_RISKS["read_tool_result"]
            )
        self._undo = UndoExecutor(context)

    def register(self, handler: ToolHandler, *, origin: ToolOrigin) -> None:
        """注册一个带来源和风险声明的处理器；任何冲突都拒绝覆盖。"""

        if not isinstance(handler, ToolHandler):
            raise ValueError("动态工具必须实现 ToolHandler")
        if not isinstance(origin, ToolOrigin):
            raise ValueError("动态工具必须声明有效 ToolOrigin")
        if handler.context is not self.context:
            raise ValueError("动态工具必须绑定当前 ToolRegistry context")
        name = getattr(handler, "name", None)
        if not isinstance(name, str) or not _TOOL_NAME_PATTERN.fullmatch(name):
            raise ValueError("工具名称必须是规范化标识")
        definition = handler.definition()
        if definition.name != name:
            raise ValueError("工具定义名称与处理器名称不一致")
        validated_schema = self._validate_definition_schema(definition.parameters)
        definition = ToolDefinition(
            definition.name,
            definition.description,
            validated_schema,
        )
        with self._registration_lock:
            if name in self._handlers or name in self._origins:
                raise ValueError(f"工具名称冲突：{name}")
            self._handlers[name] = handler
            self._origins[name] = origin
            self._definitions[name] = copy.deepcopy(definition)

    def unregister(self, handler: ToolHandler) -> bool:
        """仅撤销当前仍由同一 handler 身份占有的动态注册。"""

        if not isinstance(handler, ToolHandler):
            return False
        name = getattr(handler, "name", None)
        if not isinstance(name, str):
            return False
        with self._registration_lock:
            if self._handlers.get(name) is not handler:
                return False
            origin = self._origins.get(name)
            if origin is None or origin.kind == "builtin":
                return False
            del self._handlers[name]
            del self._origins[name]
            self._definitions.pop(name, None)
            return True

    def is_registered(self, handler: ToolHandler) -> bool:
        """判断当前名称是否仍由同一个 handler 对象占有。"""

        if not isinstance(handler, ToolHandler):
            return False
        name = getattr(handler, "name", None)
        if not isinstance(name, str):
            return False
        with self._registration_lock:
            return self._handlers.get(name) is handler

    @staticmethod
    def _validate_definition_schema(schema: object) -> dict[str, object]:
        """注册期冻结共享递归 Schema 子集，不伪造一组运行参数。"""

        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("工具参数 Schema 必须是 object")
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError("工具参数 Schema 缺少 properties 或 required")
        if schema.get("additionalProperties") is not False:
            raise ValueError("工具参数 Schema 必须拒绝额外字段")
        if not all(isinstance(name, str) and name in properties for name in required):
            raise ValueError("工具参数 Schema 的 required 无效")
        return validate_mcp_schema(schema)

    def origin(self, name: str) -> ToolOrigin:
        """返回工具的安全来源元数据；未知名称拒绝伪造默认来源。"""

        with self._registration_lock:
            try:
                return self._origins[name]
            except KeyError as exc:
                raise ValueError(f"未知工具：{name}") from exc

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回顺序固定且与内部注册表隔离的公开工具定义。"""
        with self._registration_lock:
            definitions = tuple(
                copy.deepcopy(definition)
                for definition in self._definitions.values()
            )
        if self.context.spill_store is not None:
            return (*definitions, copy.deepcopy(_READ_TOOL_RESULT_DEFINITION))
        return definitions

    def contains(self, name: str) -> bool:
        """判断名称是否在公开注册表中。"""
        with self._registration_lock:
            return name in self._handlers or (
                name == "read_tool_result" and self.context.spill_store is not None
            )

    def describe(self, name: str) -> ToolDefinition | None:
        """只返回注册表中静态声明的公开定义。"""
        if name == "read_tool_result" and self.context.spill_store is not None:
            return copy.deepcopy(_READ_TOOL_RESULT_DEFINITION)
        with self._registration_lock:
            definition = self._definitions.get(name)
        return None if definition is None else copy.deepcopy(definition)

    def requires_approval(self, name: str) -> bool:
        """根据统一风险声明判断工具是否会进入人工审批边界。"""

        with self._registration_lock:
            origin = self._origins.get(name)
        return origin is not None and origin.risk != "read"

    def retain_cleanup(self, scope: TaskCleanup) -> None:
        """独立 task 结束后接管 exact scope；正常返回也不能丢失资源所有权。"""
        scope.handoff_to(self._standalone_cleanup)

    @property
    def has_pending_cleanup(self) -> bool:
        return self._standalone_cleanup.blocks(current_cleanup())

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if self.has_pending_cleanup:
            return tool_failure(ErrorCode.CLEANUP_FAILED, "旧任务资源清理尚未确认，禁止复用执行资源")
        if name == "read_tool_result":
            return self._read_spilled_result(arguments)
        prepared = self._prepare_execution(name, arguments)
        if isinstance(prepared, ToolResult):
            return prepared
        handler, origin = prepared
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        with task_cleanup_scope(self.retain_cleanup, worker_owner=self._standalone_cleanup), \
             cancellation_scope(cancellation), self._effect_scope(name, handler) as operation:
            self._observe_dispatch(name, handler, origin, cancellation)
            try:
                if name == "run_command" and isinstance(handler, RunCommandTool):
                    result = handler.run_with_cancellation(arguments, cancellation)
                elif origin.kind == "mcp":
                    mcp_handler_class = _mcp_handler_class()
                    if type(handler) is mcp_handler_class:
                        result = mcp_handler_class.run(handler, arguments)
                    else:
                        result = handler.run(arguments)
                else:
                    result = handler.run(arguments)
                result = self._validate_execution_result(result, name, handler)
            except Exception as exc:
                result = self._safe_execution_failure(name, arguments, handler, exc)
            result = self._normalize_file_effects(name, handler, result, operation)
            result = self._normalize_error_recovery(result)
            self._publish_result(name, handler, result)
        return self._normalize_execution_result(name, arguments, result, call_id)

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        """在当前事件循环中调度处理器异步入口，并共用同步安全边界。"""

        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if self.has_pending_cleanup:
            return tool_failure(ErrorCode.CLEANUP_FAILED, "旧任务资源清理尚未确认，禁止复用执行资源")
        if name == "read_tool_result":
            return self._read_spilled_result(arguments)
        prepared = self._prepare_execution(name, arguments)
        if isinstance(prepared, ToolResult):
            return prepared
        handler, origin = prepared
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        with task_cleanup_scope(self.retain_cleanup, worker_owner=self._standalone_cleanup), \
             cancellation_scope(cancellation), self._effect_scope(name, handler) as operation:
            self._observe_dispatch(name, handler, origin, cancellation)
            try:
                if origin.kind == "mcp":
                    mcp_handler_class = _mcp_handler_class()
                    if type(handler) is mcp_handler_class:
                        result = await mcp_handler_class.run_async(
                            handler, arguments, cancellation=cancellation
                        )
                    else:
                        result = await handler.run_async(arguments, cancellation=cancellation)
                else:
                    result = await handler.run_async(arguments, cancellation=cancellation)
                result = self._validate_execution_result(result, name, handler)
            except Exception as exc:
                result = self._safe_execution_failure(name, arguments, handler, exc)
            result = self._normalize_file_effects(name, handler, result, operation)
            result = self._normalize_error_recovery(result)
            self._publish_result(name, handler, result)
        return self._normalize_execution_result(name, arguments, result, call_id)

    def _observe_dispatch(self, name: str, handler: ToolHandler, origin: ToolOrigin,
                          cancellation: CancellationToken | None) -> None:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if self._builtin_handlers.get(name) is not handler and origin.risk != "read":
            observation = current_task_observation()
            if observation is not None:
                # Schema/权限/审批及最后一次预取消检查之后，已交给 handler。
                # 异步入口内部可能先检查取消；越过此交付边界仍保守视为 UNKNOWN。
                observation.observe_effects(FileEffects(EffectState.UNKNOWN))

    def _publish_result(self, name: str, handler: ToolHandler, result: ToolResult) -> None:
        observation = current_task_observation()
        if observation is None:
            return
        candidate = result.verification_evidence
        if not (self._builtin_handlers.get(name) is handler and name == "run_command"
                and type(handler) is RunCommandTool and self.context.verification_scope.owns(candidate)):
            candidate = None
        observation.observe_result(result.file_effects or FileEffects(EffectState.NONE), candidate)

    def _effect_scope(
        self, name: str, handler: ToolHandler,
    ) -> AbstractContextManager[ChangeJournal | None]:
        if (self._builtin_handlers.get(name) is handler
                and type(handler) in (EditFileTool, CreateFileTool, ApplyPatchTool)):
            return handler.collect_effects()
        return nullcontext(None)

    def _normalize_file_effects(
        self, name: str, handler: ToolHandler, result: ToolResult,
        operation: ChangeJournal | None,
    ) -> ToolResult:
        """证据仅来自本地内置对象与操作账本；扩展自报字段没有信任权限。"""

        if self._builtin_handlers.get(name) is not handler:
            # 扩展不能伪造本地审计/暂存/验证证明；真正的 spill 在后续本地预算层形成。
            external_effects = (FileEffects(EffectState.UNKNOWN)
                                if self._origins[name].risk != "read" else None)
            return replace(result, file_effects=external_effects, relative_path=None, modified_paths=(),
                           audit_paths=(), change_chars=0, verification_passed=None,
                           verification_evidence=None,
                           spill_reference=None, spill_bytes=0, spill_sha256=None)
        if name == "run_command" and type(handler) is RunCommandTool:
            evidence = result.verification_evidence
            if not self.context.verification_scope.owns(evidence):
                result = replace(result, verification_evidence=None)
            # 只有本地命令路径持有的快照证据可将运行后文件影响收窄为 NONE。
            effects = (FileEffects(EffectState.UNKNOWN) if self.context.verification_scope.unknown_effects
                       else result.file_effects or FileEffects(EffectState.NONE))
            return replace(result, file_effects=effects)
        result = replace(result, verification_evidence=None, verification_passed=None)
        effects = operation.active_effects() if operation is not None else None
        if effects is None:
            legacy = tuple(dict.fromkeys(
                ((result.relative_path,) if result.relative_path else ()) + result.modified_paths
            )) if result.ok else ()
            effects = FileEffects(EffectState.CONFIRMED, legacy) if legacy else FileEffects(EffectState.NONE)
        paths = []
        uncertain = effects.state is EffectState.UNKNOWN
        for path in effects.paths:
            try:
                resolved = self.context.workspace_policy.resolve_path(path, must_exist=False)
                canonical = resolved.relative_to(self.context.workspace_policy.workspace).as_posix()
                if canonical != path:
                    raise ValueError("副作用路径不是规范路径")
                paths.append(canonical)
            except (PolicyError, OSError, ValueError):
                uncertain = True
        state = EffectState.UNKNOWN if uncertain else EffectState.CONFIRMED if paths else EffectState.NONE
        return replace(result, file_effects=FileEffects(state, tuple(paths)))

    def _prepare_execution(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[ToolHandler, ToolOrigin] | ToolResult:
        """取消之外统一查找、冻结 Schema 校验、只读边界与扩展审批。"""

        with self._registration_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return tool_failure(ErrorCode.UNKNOWN_TOOL, "未知工具（unknown_tool）：未注册名称")
            definition = self._definitions[name]
            origin = self._origins[name]
        try:
            ToolHandler._validate_arguments(definition.parameters, arguments)
        except (ValueError, TypeError):
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "工具参数不满足 Schema")
        if self._builtin_handlers.get(name) is handler:
            # NUL 是路径格式错误，不是执行阶段的未知 ValueError；必须在访问磁盘前拒绝。
            for field in ("path", "cwd"):
                value = arguments.get(field)
                if isinstance(value, str) and "\x00" in value:
                    return tool_failure(ErrorCode.INVALID_ARGUMENT, "路径格式无效")
        if self._builtin_handlers.get(name) is not handler:
            if self.context.read_only and origin.risk != "read":
                return tool_failure(ErrorCode.POLICY_DENIED, "只读模式禁止执行非只读扩展工具")
            if origin.risk != "read":
                approval_action = (
                    "dangerous_extension_tool"
                    if origin.risk == "dangerous"
                    else name
                )
                detail = f"扩展：{origin.id}\n工具：{name}\n风险：{origin.risk}"
                if not self.context.approver(approval_action, detail):
                    return tool_failure(ErrorCode.APPROVAL_DENIED, "用户拒绝执行扩展工具")
        return handler, origin

    def _normalize_execution_result(
        self,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        call_id: str | None,
    ) -> ToolResult:
        """统一装配 patch 审计元数据与 output/spill 预算。"""

        result = self._apply_output_budget(result, call_id)
        if name != "apply_patch" or self.context.read_only:
            return result
        audit_paths, change_chars = self._safe_patch_audit_metadata(arguments)
        return replace(
            result,
            audit_paths=audit_paths,
            change_chars=change_chars,
        )

    def _validate_execution_result(self, result: object, name: str, handler: ToolHandler) -> ToolResult:
        """在归一化前拒绝不符合公开 ToolResult 契约的处理器返回值。"""

        if (type(result) is not ToolResult or not isinstance(result.output, str)
                or type(result.ok) is not bool):
            raise _InvalidToolResultError("工具处理器返回了无效结果")
        if result.ok and result.error is not None:
            raise _InvalidToolResultError("成功结果不能携带错误")
        if self._builtin_handlers.get(name) is handler:
            if not result.ok and result.error is None:
                result = replace(result, error=ToolError(ErrorCode.EXECUTION_FAILED, RecoveryAction.REPLAN))
            if result.error is not None and type(result.error) is not ToolError:
                raise _InvalidToolResultError("工具错误结构无效")
        elif not result.ok:
            # MCP 的本地适配器直接调用受控入口；普通扩展自报同名类型没有证明力。
            origin = self._origins.get(name)
            if (origin is None or origin.kind != "mcp"
                    or type(handler) is not _mcp_handler_class()
                    or type(result.error) is not ToolError):
                raise _InvalidToolResultError("扩展失败结构不可信")
        return result

    @staticmethod
    def _normalize_error_recovery(result: ToolResult) -> ToolResult:
        """先核对 T1 副作用，再收紧恢复建议，绝不清除已知路径或 UNKNOWN。"""
        if (not result.ok and result.file_effects is not None
                and result.file_effects.state is EffectState.UNKNOWN):
            return replace(result, error=replace(result.error, recovery=RecoveryAction.STOP_TASK))
        return result

    def _safe_execution_failure(
        self,
        name: str,
        arguments: dict[str, Any],
        handler: ToolHandler,
        exc: Exception,
    ) -> ToolResult:
        """保持既有预期错误语义，并对扩展未知异常执行固定脱敏。"""

        if isinstance(exc, CancellationError):
            raise exc
        if self._builtin_handlers.get(name) is not handler:
            code = (ErrorCode.INVALID_RESULT if isinstance(exc, _InvalidToolResultError)
                    else ErrorCode.RESULT_UNCERTAIN)
            return tool_failure(code, "扩展工具执行失败，已安全隔离")
        if isinstance(exc, ChangeBudgetError):
            return tool_failure(ErrorCode.POLICY_DENIED, "变更预算不足，拒绝操作")
        if isinstance(exc, PolicyArgumentError):
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "命令或路径格式无效")
        if isinstance(exc, PolicyError):
            return tool_failure(ErrorCode.POLICY_DENIED, "本地安全策略拒绝操作（路径、命令或安全目录绑定不满足要求）")
        if isinstance(exc, InvalidToolArgument):
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "工具参数格式无效")
        if isinstance(exc, (OSError, UnicodeError)):
            if name == "apply_patch" and not self.context.read_only:
                return tool_failure(ErrorCode.EXECUTION_FAILED, self._safe_patch_filesystem_error(arguments))
            return tool_failure(ErrorCode.EXECUTION_FAILED, "文件系统操作失败")
        raise exc

    def _read_spilled_result(self, arguments: dict[str, Any]) -> ToolResult:
        store = self.context.spill_store
        if store is None:
            return tool_failure(ErrorCode.UNKNOWN_TOOL, "当前 Session 没有可回读的大型工具结果")
        try:
            ToolHandler._validate_arguments(
                _READ_TOOL_RESULT_DEFINITION.parameters,
                arguments,
            )
            reference = arguments.get("reference")
            offset = arguments.get("offset", 0)
            if not isinstance(reference, str):
                raise ValueError("reference 必须是 string")
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise ValueError("offset 必须是 integer")
            return ToolResult(
                True,
                store.preview(
                    reference,
                    offset=offset,
                    max_chars=self.context.max_output_chars,
                ),
            )
        except (SpillError, ValueError, TypeError):
            return tool_failure(ErrorCode.INVALID_ARGUMENT, "大型工具结果引用无效或不可读取")

    def _apply_output_budget(
        self,
        result: ToolResult,
        call_id: str | None,
    ) -> ToolResult:
        """大结果优先 spill；失败时退回旧式截断，绝不返回无界正文。"""

        if len(result.output) <= self.context.max_output_chars:
            return result
        store = self.context.spill_store
        if store is None:
            return self._truncate_inline_result(result)
        try:
            record = store.persist(
                call_id or f"local-{secrets.token_hex(16)}",
                result.output,
            )
            preview_chars = min(2_000, self.context.max_output_chars)
            preview = store.preview(record.reference, max_chars=preview_chars)
        except SpillError:
            return self._truncate_inline_result(result)
        instruction = (
            "工具输出过大，完整内容已安全暂存。\n"
            f"引用：{record.reference}\n"
            f"大小：{record.byte_count} bytes\n"
            f"预览：\n{preview}\n"
            "如需后续内容，调用 read_tool_result，并传入 reference 与 offset。"
        )
        return replace(
            result,
            output=instruction,
            spill_reference=record.reference,
            spill_bytes=record.byte_count,
            spill_sha256=record.sha256,
        )

    def _truncate_inline_result(self, result: ToolResult) -> ToolResult:
        """无法安全暂存时有界降级；提示文字本身也计入内联预算。"""

        limit = max(0, self.context.max_output_chars)
        marker = "\n...（输出已截断，完整结果未暂存）"
        prefix_chars = max(0, limit - len(marker))
        return replace(result, output=(result.output[:prefix_chars] + marker)[:limit])

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
