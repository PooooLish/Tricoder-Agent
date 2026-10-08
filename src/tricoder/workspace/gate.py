"""任务开始前的工作区快照比较与精确确认门禁实现。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tricoder.core.cancellation import CancellationToken
from tricoder.workspace.baseline_record import (
    BaselineComparison,
    BaselineRecord,
    BaselineRecordError,
    compare_baseline_records,
    make_baseline_record,
)
from tricoder.workspace.snapshot import (
    SnapshotLimits,
    WorkspaceBaseline,
    WorkspaceChangePreview,
    capture_workspace_baseline,
    compare_baselines,
)


class WorkspaceGateError(RuntimeError):
    """工作区门禁无法安全放行。"""


class WorkspaceConfirmationUnavailable(WorkspaceGateError):
    """当前入口没有显式确认能力。"""

    def __init__(self, kind: str = "changed") -> None:
        self.kind = kind
        super().__init__(kind)


class WorkspaceConfirmationRejected(WorkspaceGateError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


class WorkspaceGateCancelled(WorkspaceGateError):
    """确认前后任务已取消。"""


@dataclass(frozen=True, slots=True)
class WorkspaceGatePreview:
    preview_id: str
    request_id: str
    generation: int
    session_id: str
    workspace_key: str
    baseline_id: str | None
    candidate_id: str
    kind: str
    changed_paths: tuple[str, ...]
    pages: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class WorkspaceGateDecision:
    baseline: WorkspaceBaseline
    changed: bool
    preview: WorkspaceGatePreview | None = None


Snapshotter = Callable[..., WorkspaceBaseline]
Confirmer = Callable[[WorkspaceGatePreview], bool]
ChangeObserver = Callable[[WorkspaceGatePreview], None]


class WorkspaceGate:
    """扫描、比较、确认和确认后复扫均在调用方持有工作区锁时完成。"""

    def __init__(
        self,
        *,
        snapshotter: Snapshotter = capture_workspace_baseline,
        limits: SnapshotLimits | None = None,
        max_confirmations: int = 8,
    ) -> None:
        self._snapshotter = snapshotter
        self._limits = limits or SnapshotLimits()
        self._max_confirmations = max_confirmations

    def enter(
        self,
        root: Path,
        *,
        baseline: WorkspaceBaseline | None,
        require_initialization_confirmation: bool,
        persisted_baseline: BaselineRecord | None = None,
        session_id: str,
        request_id: str,
        generation: int,
        confirmer: Confirmer | None,
        cancellation: CancellationToken | None,
        change_observer: ChangeObserver | None = None,
    ) -> WorkspaceGateDecision:
        candidate = self._capture(root, cancellation)
        if baseline is None and persisted_baseline is None and not require_initialization_confirmation:
            return WorkspaceGateDecision(candidate, changed=False)

        last_preview: WorkspaceGatePreview | None = None
        for _attempt in range(self._max_confirmations):
            self._raise_if_cancelled(cancellation)
            if baseline is None and persisted_baseline is None:
                preview = WorkspaceGatePreview(
                    preview_id=secrets.token_hex(16),
                    request_id=request_id,
                    generation=generation,
                    session_id=session_id,
                    workspace_key=candidate.workspace_key,
                    baseline_id=None,
                    candidate_id=candidate.snapshot_id,
                    kind="initialize",
                    changed_paths=(),
                    pages=(),
                    message="恢复的会话没有内存代码快照，无法比较重启期间的历史修改；确认以当前完整扫描初始化基线",
                )
            else:
                try:
                    candidate_record = make_baseline_record(candidate)
                    reference_record = (
                        make_baseline_record(baseline)
                        if baseline is not None
                        else persisted_baseline
                    )
                    assert reference_record is not None
                    stable_comparison = compare_baseline_records(
                        reference_record,
                        candidate_record,
                    )
                except (BaselineRecordError, TypeError, ValueError) as exc:
                    raise WorkspaceGateError("工作区基线比较失败，任务未启动") from exc
                if not stable_comparison.changed:
                    return WorkspaceGateDecision(candidate, changed=False)
                if baseline is not None and stable_comparison.compatible:
                    try:
                        comparison = compare_baselines(baseline, candidate)
                    except (TypeError, ValueError) as exc:
                        raise WorkspaceGateError(
                            "工作区快照身份或范围发生变化，任务未启动"
                        ) from exc
                    preview = self._changed_preview(
                        comparison,
                        request_id=request_id,
                        generation=generation,
                        session_id=session_id,
                    )
                else:
                    preview = self._persistent_preview(
                        stable_comparison,
                        reference_record,
                        candidate_record,
                        request_id=request_id,
                        generation=generation,
                        session_id=session_id,
                    )
                if change_observer is not None:
                    change_observer(preview)
            if confirmer is None:
                raise WorkspaceConfirmationUnavailable(preview.kind)
            approved = confirmer(preview)
            self._raise_if_cancelled(cancellation)
            if approved is not True:
                raise WorkspaceConfirmationRejected(preview.kind)
            rescanned = self._capture(root, cancellation)
            self._raise_if_cancelled(cancellation)
            if rescanned.snapshot_id == candidate.snapshot_id:
                return WorkspaceGateDecision(
                    rescanned,
                    changed=(baseline is not None or persisted_baseline is not None),
                    preview=preview,
                )
            candidate = rescanned
            last_preview = preview
        raise WorkspaceGateError(
            "工作区在确认期间持续变化，任务未启动"
            if last_preview is not None
            else "工作区门禁无法完成"
        )

    @staticmethod
    def _persistent_preview(
        comparison: BaselineComparison,
        baseline: BaselineRecord,
        candidate: BaselineRecord,
        *,
        request_id: str,
        generation: int,
        session_id: str,
    ) -> WorkspaceGatePreview:
        """为跨进程记录生成不含源码正文的文件级预览。"""

        labels = {
            "added": "新增",
            "deleted": "删除",
            "modified": "修改",
            "type_changed": "类型变化",
            "permission_changed": "权限变化",
        }
        lines = [
            f"{labels.get(change.change_type, '变化')}  {change.path}"
            for change in comparison.changes
        ]
        if not comparison.compatible:
            reason_labels = {
                "workspace_mismatch": "工作区绑定不一致",
                "scope_version_changed": "扫描范围版本变化",
                "root_identity_changed": "工作区根身份变化",
            }
            lines = [reason_labels.get(comparison.reason, "持久化基线不兼容")]
        pages = tuple(
            "\n".join(lines[index:index + 50])
            for index in range(0, len(lines), 50)
        )
        return WorkspaceGatePreview(
            preview_id=secrets.token_hex(16),
            request_id=request_id,
            generation=generation,
            session_id=session_id,
            workspace_key=candidate.workspace_key,
            baseline_id=baseline.content_digest,
            candidate_id=candidate.content_digest,
            kind="changed" if comparison.compatible else "incompatible",
            changed_paths=comparison.changed_paths,
            pages=pages,
            message=(
                "工作区相对于本 Session 上次持久化基线发生变化；"
                "跨重启仅提供文件级差异，确认只接受当前候选作为操作起点"
            ),
        )

    def _capture(
        self,
        root: Path,
        cancellation: CancellationToken | None,
    ) -> WorkspaceBaseline:
        return self._snapshotter(root, self._limits, cancellation=cancellation)

    def capture_current(
        self,
        root: Path,
        *,
        cancellation: CancellationToken | None,
    ) -> WorkspaceBaseline:
        """供任务收尾复用同一范围和失败语义。"""

        return self._capture(root, cancellation)

    @staticmethod
    def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
        if cancellation is not None and cancellation.is_cancelled:
            raise WorkspaceGateCancelled("任务已取消，工作区确认失效")

    @staticmethod
    def _changed_preview(
        comparison: WorkspaceChangePreview,
        *,
        request_id: str,
        generation: int,
        session_id: str,
    ) -> WorkspaceGatePreview:
        return WorkspaceGatePreview(
            preview_id=secrets.token_hex(16),
            request_id=request_id,
            generation=generation,
            session_id=session_id,
            workspace_key=comparison.workspace_key,
            baseline_id=comparison.baseline_id,
            candidate_id=comparison.candidate_id,
            kind="changed",
            changed_paths=comparison.changed_paths,
            pages=comparison.pages,
            message="工作区代码自本 Session 上次可信快照后发生变化；确认只接受其作为本任务起点",
        )
