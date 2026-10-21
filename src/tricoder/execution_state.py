"""文件副作用与执行状态的不可变契约；不访问文件系统。"""

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath


class TaskStopReason(str, Enum):
    """Host-observed terminal reason; not a completion capability."""

    COMPLETED = "completed"
    MODEL_INCOMPLETE = "model_incomplete"
    ROUND_BUDGET_EXHAUSTED = "round_budget_exhausted"
    NEEDS_INPUT = "needs_input"
    TOOL_STOP = "tool_stop"
    VERIFICATION_BLOCKED = "verification_blocked"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"
    UNKNOWN_EFFECTS = "unknown_effects"
    PROGRESS_STOPPED = "progress_stopped"
    PROVIDER_FAILED = "provider_failed"
    RUNTIME_REJECTED = "runtime_rejected"


class ErrorCode(str, Enum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENT = "invalid_argument"
    POLICY_DENIED = "policy_denied"
    APPROVAL_DENIED = "approval_denied"
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    RESULT_UNCERTAIN = "result_uncertain"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"
    INVALID_RESULT = "invalid_result"
    SKIPPED = "skipped"
    NEEDS_INPUT = "needs_input"


class RecoveryAction(str, Enum):
    REPLAN = "replan"
    STOP_TASK = "stop_task"


PATCH_ERROR_DETAILS = frozenset({
    "patch_invalid_header", "patch_invalid_hunk", "patch_count_mismatch",
    "patch_unsupported_operation", "patch_context_mismatch", "patch_invalid_format",
})
TOOL_ERROR_DETAILS = PATCH_ERROR_DETAILS | {"git_workspace_unavailable", "file_already_exists"}


@dataclass(frozen=True, slots=True)
class ToolError:
    code: ErrorCode
    recovery: RecoveryAction
    retryable: bool = False
    detail_code: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.code, ErrorCode)
                or not isinstance(self.recovery, RecoveryAction)
                or self.retryable is not False):
            raise ValueError("工具错误契约无效；尚不支持自动重试")
        if self.detail_code is not None and (
            not isinstance(self.detail_code, str) or self.detail_code not in TOOL_ERROR_DETAILS
        ):
            raise ValueError("工具错误子类型无效")

    def public_fields(self) -> dict[str, str | bool]:
        """协议、展示和审计仅消费本地产生的稳定字段，不附带异常正文。"""
        fields: dict[str, str | bool] = {
            "code": self.code.value, "recovery": self.recovery.value,
            "retryable": self.retryable,
        }
        if self.detail_code is not None:
            fields["detail_code"] = self.detail_code
        return fields


class EffectState(str, Enum):
    NONE = "none"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FileEffects:
    state: EffectState
    paths: tuple[str, ...] = ()
    # 保留类名与原有第二位置参数兼容；目录路径始终独立于普通文件路径。
    directory_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.state, EffectState)
            or not isinstance(self.paths, tuple)
            or not isinstance(self.directory_paths, tuple)
        ):
            raise ValueError("文件副作用状态无效")
        if self.state is EffectState.NONE and (self.paths or self.directory_paths):
            raise ValueError("无副作用状态不能携带路径")
        if self.state is EffectState.CONFIRMED and not (
            self.paths or self.directory_paths
        ):
            raise ValueError("已确认副作用必须携带路径")
        if len(self.directory_paths) > 128:
            raise ValueError("目录副作用路径超过单任务上限")
        if len(set(self.paths)) != len(self.paths) or len(set(self.directory_paths)) != len(
            self.directory_paths
        ):
            raise ValueError("副作用路径不能重复")
        for path in (*self.paths, *self.directory_paths):
            if (not isinstance(path, str) or not path or "\\" in path
                    or not all(character.isprintable() for character in path)
                    or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive
                    or any(part in ("", ".", "..") for part in path.split("/"))):
                raise ValueError("副作用路径必须是规范相对路径")


def should_stop_task(error: ToolError | None, effects: FileEffects) -> bool:
    """根错误决定恢复动作；未知文件后态无条件停止，不解析输出正文。"""
    return (
        effects.state is EffectState.UNKNOWN
        or (error is not None and error.recovery is RecoveryAction.STOP_TASK)
    )


@dataclass(frozen=True, slots=True)
class ExecutionState:
    modified_files: tuple[str, ...] = ()
    verification: str = "未运行"
    unknown_effects: bool = False
    modified_directories: tuple[str, ...] = ()

    def observe(self, effects: FileEffects) -> "ExecutionState":
        if not isinstance(effects, FileEffects):
            raise ValueError("文件副作用证据无效")
        verification = self.verification
        if effects.state is EffectState.UNKNOWN or effects.paths:
            verification = "待验证"
        elif effects.directory_paths and verification in {"通过", "passed"}:
            verification = "待验证"
        return ExecutionState(
            tuple(dict.fromkeys((*self.modified_files, *effects.paths))),
            verification,
            self.unknown_effects or effects.state is EffectState.UNKNOWN,
            tuple(
                dict.fromkeys(
                    (*self.modified_directories, *effects.directory_paths)
                )
            ),
        )
