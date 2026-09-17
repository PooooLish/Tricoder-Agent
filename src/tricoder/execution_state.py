"""文件副作用与执行状态的不可变契约；不访问文件系统。"""

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath


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


class RecoveryAction(str, Enum):
    REPLAN = "replan"
    STOP_TASK = "stop_task"


@dataclass(frozen=True, slots=True)
class ToolError:
    code: ErrorCode
    recovery: RecoveryAction
    retryable: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.code, ErrorCode)
                or not isinstance(self.recovery, RecoveryAction)
                or self.retryable is not False):
            raise ValueError("工具错误契约无效；尚不支持自动重试")

    def public_fields(self) -> dict[str, str | bool]:
        """协议、展示和审计仅消费本地产生的稳定字段，不附带异常正文。"""
        return {"code": self.code.value, "recovery": self.recovery.value,
                "retryable": self.retryable}


class EffectState(str, Enum):
    NONE = "none"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FileEffects:
    state: EffectState
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, EffectState) or not isinstance(self.paths, tuple):
            raise ValueError("文件副作用状态无效")
        if self.state is EffectState.NONE and self.paths:
            raise ValueError("无副作用状态不能携带路径")
        if self.state is EffectState.CONFIRMED and not self.paths:
            raise ValueError("已确认副作用必须携带路径")
        for path in self.paths:
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

    def observe(self, effects: FileEffects) -> "ExecutionState":
        if not isinstance(effects, FileEffects):
            raise ValueError("文件副作用证据无效")
        return ExecutionState(
            tuple(dict.fromkeys((*self.modified_files, *effects.paths))),
            "待验证" if effects.state is not EffectState.NONE else self.verification,
            self.unknown_effects or effects.state is EffectState.UNKNOWN,
        )
