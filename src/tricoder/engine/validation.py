"""单任务检查事实聚合；不执行命令，也不判断业务需求。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from tricoder.core.validation import (
    CommandCheckRecord,
    TaskValidationReport,
    has_effective_success_result,
)


@dataclass(slots=True)
class TaskValidationTracker:
    """维护最近检查和未解决失败，所有状态仅在当前任务内存活。"""

    task_id: str
    max_records: int = 32
    _records: list[CommandCheckRecord] = field(default_factory=list, init=False)
    _unresolved: OrderedDict[tuple[tuple[str, ...], str], str] = field(
        default_factory=OrderedDict,
        init=False,
    )
    _latest: OrderedDict[tuple[tuple[str, ...], str], CommandCheckRecord] = field(
        default_factory=OrderedDict,
        init=False,
    )
    _stale: OrderedDict[tuple[tuple[str, ...], str], str] = field(
        default_factory=OrderedDict,
        init=False,
    )
    _recent_only: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("任务验证 task_id 必须为非空字符串")
        if type(self.max_records) is not int or not 1 <= self.max_records <= 32:
            raise ValueError("任务验证记录上限必须位于 1..32")

    def invalidate(self) -> None:
        """后续文件变化令已有检查过期，但不删除失败事实。"""

        for signature, record in self._latest.items():
            if record.kind != "information":
                self._stale[signature] = record.check_id

    @property
    def has_workspace_checks(self) -> bool:
        """当前有需要绑定最终工作区版本的非信息检查。"""

        return any(record.kind != "information" for record in self._latest.values())

    def reconcile_snapshot(self, snapshot_id: str | None) -> None:
        """把最终完整快照与仍保留的检查绑定；不复活已经过期的记录。"""

        for signature, record in self._latest.items():
            if (
                record.kind != "information"
                and record.workspace_stable
                and (snapshot_id is None or record.snapshot_id != snapshot_id)
            ):
                self._stale[signature] = record.check_id

    def observe(self, record: CommandCheckRecord) -> None:
        """合并一条宿主记录；跨任务记录直接拒绝。"""

        if not isinstance(record, CommandCheckRecord):
            raise TypeError("任务验证只接受 CommandCheckRecord")
        if record.task_id != self.task_id:
            raise ValueError("验证记录不属于当前任务")

        self._latest[record.signature] = record
        self._records.append(record)
        if len(self._records) > self.max_records:
            overflow = self._records[: len(self._records) - self.max_records]
            del self._records[: len(self._records) - self.max_records]
            for evicted in overflow:
                latest = self._latest.get(evicted.signature)
                if latest is not None and latest.check_id == evicted.check_id:
                    self._latest.pop(evicted.signature, None)
            self._recent_only = True
        if record.kind != "information" and record.workspace_stable:
            self._stale.pop(record.signature, None)

        conclusive_success = (
            has_effective_success_result(record)
            and (record.kind == "information" or record.workspace_stable)
        )
        if conclusive_success:
            self._unresolved.pop(record.signature, None)
        elif not record.execution_complete or record.returncode != 0:
            self._unresolved[record.signature] = record.check_id

    def report(self) -> TaskValidationReport:
        """生成不可变公开视图；状态不等同于业务验收结论。"""

        if self._stale:
            status = "stale"
        elif self._unresolved:
            status = "failed"
        elif self._records:
            status = "observed"
        else:
            status = "unverified"

        limitations = ["需求覆盖未自动确认"]
        if self._stale:
            limitations.append("检查后工作区状态已变化，旧记录已过期")
        if self._unresolved:
            limitations.append("存在未解决的检查失败")
        if self._recent_only:
            limitations.append(f"仅显示最近 {self.max_records} 条检查记录")
        for record in self._records:
            for limitation in record.limitations:
                if limitation not in limitations:
                    limitations.append(limitation)
            if "zero_tests_reported" in record.diagnostics:
                zero_tests = "命令输出报告 0 个测试，仅作诊断提示"
                if zero_tests not in limitations:
                    limitations.append(zero_tests)

        return TaskValidationReport(
            records=tuple(self._records),
            status=status,
            limitations=tuple(limitations),
            unresolved_check_ids=tuple(self._unresolved.values()),
            recent_only=self._recent_only,
        )


__all__ = ["TaskValidationTracker"]
