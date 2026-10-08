"""不依赖执行器或界面的任务验证事实模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


_CHECK_KINDS = frozenset(
    {"script", "tests", "syntax", "static", "information", "other"}
)
_REPORT_STATUSES = frozenset({"unverified", "observed", "failed", "stale"})


@dataclass(frozen=True, slots=True)
class CommandCheckRecord:
    """宿主根据一次真实命令执行生成的有界事实。

    ``snapshot_id`` 和 ``_authority`` 只用于本地时效及来源校验，不展示、
    不写入会话数据库，也不发送给 Provider。
    """

    task_id: str
    check_id: str
    argv: tuple[str, ...]
    cwd: str
    kind: str
    returncode: int | None
    output_summary: str
    # 两个布尔值均由宿主签发，不从退出码、输出或模型文本推断。
    execution_complete: bool = False
    workspace_stable: bool = False
    targets: tuple[str, ...] = ()
    snapshot_id: str | None = field(default=None, repr=False)
    limitations: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    output_truncated: bool = False
    spill_reference: str | None = None
    _authority: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("验证记录 task_id 必须为非空字符串")
        if not isinstance(self.check_id, str) or not self.check_id:
            raise ValueError("验证记录 check_id 必须为非空字符串")
        if not isinstance(self.argv, tuple) or not self.argv or not all(
            isinstance(item, str) and item for item in self.argv
        ):
            raise ValueError("验证记录 argv 必须为非空字符串元组")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError("验证记录 cwd 必须为非空字符串")
        if self.kind not in _CHECK_KINDS:
            raise ValueError("验证记录 kind 不受支持")
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("验证记录 returncode 必须为整数或 None")
        if not isinstance(self.output_summary, str):
            raise ValueError("验证记录输出摘要必须为字符串")
        if len(self.output_summary) > 2_000:
            raise ValueError("验证记录输出摘要超过上限")
        for values, label in (
            (self.targets, "targets"),
            (self.limitations, "limitations"),
            (self.diagnostics, "diagnostics"),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise ValueError(f"验证记录 {label} 必须为非空字符串元组")
        if type(self.output_truncated) is not bool:
            raise ValueError("验证记录 output_truncated 必须为布尔值")
        if type(self.execution_complete) is not bool:
            raise ValueError("验证记录 execution_complete 必须为布尔值")
        if type(self.workspace_stable) is not bool:
            raise ValueError("验证记录 workspace_stable 必须为布尔值")
        if self.workspace_stable and self.snapshot_id is None:
            raise ValueError("工作区稳定记录必须绑定完整快照")
        for value, label in (
            (self.snapshot_id, "snapshot_id"),
            (self.spill_reference, "spill_reference"),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"验证记录 {label} 必须为非空字符串或 None")

    @property
    def signature(self) -> tuple[tuple[str, ...], str]:
        """用于失败替代的规范化检查身份；不依赖模型说明。"""

        return self.argv, self.cwd


@dataclass(frozen=True, slots=True)
class TaskValidationReport:
    """单任务最近检查、未解决失败和保守覆盖限制。"""

    records: tuple[CommandCheckRecord, ...] = ()
    status: str = "unverified"
    limitations: tuple[str, ...] = ("需求覆盖未自动确认",)
    unresolved_check_ids: tuple[str, ...] = ()
    recent_only: bool = False
    requirements_coverage_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.status not in _REPORT_STATUSES:
            raise ValueError("任务验证状态不受支持")
        if len(self.records) > 32 or not all(
            isinstance(record, CommandCheckRecord) for record in self.records
        ):
            raise ValueError("任务验证记录必须是至多 32 条规范记录")
        if self.requirements_coverage_confirmed:
            raise ValueError("首版任务验证不能自动确认需求覆盖")
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(item, str) and item for item in self.limitations
        ):
            raise ValueError("任务验证限制必须为非空字符串元组")
        if not isinstance(self.unresolved_check_ids, tuple) or not all(
            isinstance(item, str) and item for item in self.unresolved_check_ids
        ):
            raise ValueError("未解决检查 ID 必须为非空字符串元组")
        if type(self.recent_only) is not bool:
            raise ValueError("recent_only 必须为布尔值")


def has_effective_success_result(record: CommandCheckRecord) -> bool:
    """判断真实成功执行是否具有验证能力；明确零测试只保留诊断。"""

    return bool(
        record.execution_complete
        and record.returncode == 0
        and "scope_filtered" not in record.diagnostics
        and not (
            record.kind == "tests" and "zero_tests_reported" in record.diagnostics
        )
    )


__all__ = [
    "CommandCheckRecord",
    "TaskValidationReport",
    "has_effective_success_result",
]
