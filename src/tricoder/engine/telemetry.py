"""Agent 观察者、事件发布和审计辅助。"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from tricoder.audit import AuditLogger
from tricoder.core.events import AgentEvent, EventSink
from tricoder.extensions.models import ToolOrigin
from tricoder.models import RunResult, TokenUsage, ToolAction, ToolResult
from tricoder.policy import PolicyError


@runtime_checkable
class AgentObserver(Protocol):
    """接收 Agent 的公开运行事件，不接触隐藏推理或凭据。"""

    def on_round_start(self, round_number: int, max_rounds: int) -> None: ...

    def on_action(self, action: ToolAction) -> None: ...

    def on_tool_result(
        self,
        action: ToolAction,
        result: Any,
        duration_ms: int,
    ) -> None: ...

    def on_error(self, message: str) -> None: ...


@runtime_checkable
class ProviderUsageObserver(Protocol):
    """可选接收逐轮归一化 Provider 用量。"""

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None: ...


def notify_provider_usage(
    observer: AgentObserver,
    round_number: int,
    usage: TokenUsage,
) -> None:
    """仅向实现了可选用量协议的观察者发布用量。"""

    if isinstance(observer, ProviderUsageObserver):
        observer.on_provider_usage(round_number, usage)


class NullObserver:
    """在库调用或测试中保持完全静默的默认观察者。"""

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        return None

    def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
        return None

    def on_action(self, action: ToolAction) -> None:
        return None

    def on_tool_result(self, action: ToolAction, result: Any, duration_ms: int) -> None:
        return None

    def on_error(self, message: str) -> None:
        return None


def emit(event_sink: EventSink | None, event: AgentEvent) -> None:
    """向可选事件接收器同步发布一个类型化事件。"""

    if event_sink is not None:
        event_sink(event)


def elapsed_ms(started: float) -> int:
    """返回自 ``started`` 起经过的整毫秒数。"""

    return round((time.perf_counter() - started) * 1000)


def log_event(
    audit: AuditLogger | None,
    observer: AgentObserver,
    event: dict[str, Any],
    *,
    failure_message: str,
) -> bool:
    """写入审计记录；写入失败时保留原异常优先级和通知语义。"""

    if audit is None:
        return True
    try:
        audit.log(event)
    except OSError as audit_error:
        try:
            observer.on_error(failure_message)
        except BaseException:
            raise audit_error
        return False
    return True


def audit_usage_event(round_number: int, usage: TokenUsage) -> dict[str, Any]:
    """把可用的 Provider 用量字段转换为审计事件。"""

    event: dict[str, Any] = {"round": round_number, "status": "provider_usage"}
    for field, value in (
        ("input", usage.input_tokens),
        ("output", usage.output_tokens),
        ("cached", usage.cached_tokens),
        ("cache_miss", usage.cache_miss_tokens),
    ):
        if value is not None:
            event[field] = value
    return event


def audit_failure_result(
    failure_message: str,
    round_number: int,
    tool_calls: int,
    modified_files: list[str],
    verification: str,
    modified_directories: list[str] | None = None,
) -> RunResult:
    """构造统一的审计失败结果，不改写已观察到的任务事实。"""

    return RunResult(
        False,
        failure_message,
        round_number,
        tool_calls,
        tuple(modified_files),
        verification,
        modified_directories=tuple(modified_directories or ()),
    )


def audit_arguments(tools: object, action: ToolAction, result: ToolResult) -> dict[str, Any]:
    """只保留审计需要的元数据，避免重复保存源码和任务摘要。"""

    arguments = action.arguments
    if not result.ok and action.tool not in {"run_command", "apply_patch"}:
        return {"argument_count": len(arguments)}
    if action.tool in {"list_files", "read_file"}:
        return {"path": arguments.get("path", ".")}
    if action.tool == "read_tool_result":
        return {
            "reference": arguments.get("reference"),
            "offset": arguments.get("offset", 0),
        }
    if action.tool == "search_text":
        query = arguments.get("query", "")
        return {
            "path": arguments.get("path", "."),
            "query_chars": len(query) if isinstance(query, str) else 0,
        }
    if action.tool == "edit_file":
        old_text = arguments.get("old_text", "")
        new_text = arguments.get("new_text", "")
        return {
            "path": arguments.get("path"),
            "old_text_chars": len(old_text) if isinstance(old_text, str) else 0,
            "new_text_chars": len(new_text) if isinstance(new_text, str) else 0,
        }
    if action.tool == "create_file":
        content = arguments.get("content", "")
        return {
            "path": arguments.get("path"),
            "content_chars": len(content) if isinstance(content, str) else 0,
            "create_parents": arguments.get("create_parents", False),
        }
    if action.tool == "create_directory":
        return {
            "path": arguments.get("path"),
            "parents": arguments.get("parents", True),
            "exist_ok": arguments.get("exist_ok", True),
        }
    if action.tool == "apply_patch":
        patch_text = arguments.get("patch", "")
        return {
            "patch_chars": len(patch_text) if isinstance(patch_text, str) else 0,
            "paths": result.audit_paths,
            "file_count": len(result.audit_paths),
            "change_chars": result.change_chars,
        }
    if action.tool == "run_command":
        context = getattr(tools, "context")
        command = arguments.get("command", "")
        cwd = arguments.get("cwd", ".")
        if isinstance(cwd, str):
            try:
                resolved_cwd = context.workspace_policy.resolve_path(cwd)
                if not resolved_cwd.is_dir():
                    raise PolicyError("命令工作目录必须是目录")
                relative_cwd = resolved_cwd.relative_to(context.workspace_policy.workspace)
                command_policy = context.command_policy.scoped_to(
                    context.workspace_policy
                )
                metadata = command_policy.audit_metadata(
                    command if isinstance(command, str) else "",
                    cwd=resolved_cwd,
                )
                metadata["cwd"] = {
                    "is_workspace": not relative_cwd.parts,
                    "depth": len(relative_cwd.parts),
                }
            except (PolicyError, ValueError):
                metadata = {
                    "command_valid": False,
                    "command_chars": len(command) if isinstance(command, str) else 0,
                    "cwd": {"valid": False, "chars": len(cwd)},
                }
        else:
            metadata = {
                "command_valid": False,
                "command_chars": len(command) if isinstance(command, str) else 0,
                "cwd": {"valid": False, "chars": 0},
            }
        return metadata
    if action.tool == "finish":
        summary = arguments.get("summary", "")
        return {"summary_chars": len(summary) if isinstance(summary, str) else 0}
    return {"argument_count": len(arguments)}


def tool_origin_event(tools: object, tool_name: str) -> dict[str, str] | None:
    """读取经过注册表验证的工具来源；无效来源不影响主流程。"""

    origin_resolver = getattr(tools, "origin", None)
    contains = getattr(tools, "contains")
    if not callable(origin_resolver) or not contains(tool_name):
        return None
    try:
        origin = origin_resolver(tool_name)
    except (TypeError, ValueError):
        return None
    if not isinstance(origin, ToolOrigin):
        return None
    return {"kind": origin.kind, "id": origin.id, "risk": origin.risk}
