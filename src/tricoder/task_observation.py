"""异常收尾可消费的本地任务事实；不接受工具参数，不持久化或公开给扩展。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from tricoder.execution_state import EffectState, ExecutionState, FileEffects
from tricoder.models import SessionContext
from tricoder.verification import VerificationEvidence, proves_new_file_version, stable_snapshots


def apply_tool_transition(state: SessionContext, effects: FileEffects,
                          candidate: VerificationEvidence | None = None) -> SessionContext:
    """纯转换：调用方须先核验 builtin 身份/authority；重复消费同一事实幂等。"""
    observed = ExecutionState(state.modified_files, state.verification, state.unknown_effects).observe(effects)
    evidence, failure, required = state.verification_evidence, state.verification_failure, state.verification_required
    verification = observed.verification
    if effects.state is not EffectState.NONE:
        evidence, required = None, True
        if effects.state is EffectState.CONFIRMED:
            failure = None
    if candidate is not None:
        required = True
        if failure is not None and proves_new_file_version(failure, candidate.before):
            failure = None
        if stable_snapshots(candidate.before, candidate.after) and not candidate.passed:
            failure, evidence, verification = candidate.after, None, "失败"
        elif candidate.passed and effects.state is EffectState.NONE:
            evidence = candidate
            verification = "失败" if failure is not None else "通过"
        else:
            evidence, verification = None, "待验证"
    return replace(state, modified_files=observed.modified_files, verification=verification,
                   unknown_effects=observed.unknown_effects, verification_evidence=evidence,
                   verification_failure=failure, verification_required=required)


class TaskObservation:
    """每任务一个通道；None 表示尚未发布，不等于已发布的 evidence=None。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: SessionContext | None = None
        self._has_publication = False
        self._agent_published = False
        self._effects_observed = False
        self._consumed_revision: int | None = None
        self._unknown = False
        self._closed = False

    def seed(self, state: SessionContext, *, journal_revision: int) -> None:
        """Runtime 在任何 Agent 前建立基线；基线不是一条新工具事实。"""
        with self._lock:
            if not self._closed and self._state is None:
                self._state = replace(state, messages=(), persisted_summary="")
                self._consumed_revision = journal_revision
                self._unknown = state.unknown_effects

    def begin_tool(self) -> None:
        with self._lock:
            if not self._closed:
                # 未返回工具结果不等于旧提交未消费；修订游标保持不变。
                self._effects_observed = False

    def observe_effects(self, effects: FileEffects | None) -> None:
        with self._lock:
            if not self._closed and effects is not None and effects.state is EffectState.UNKNOWN:
                self._unknown = True

    def observe_result(self, effects: FileEffects, candidate: VerificationEvidence | None) -> None:
        """Registry 在首个可抛后处理前发布已归一化、已本地核验的事实。"""
        with self._lock:
            if not self._closed:
                self._state = apply_tool_transition(self._state or SessionContext(), effects, candidate)
                self._has_publication = True
                # 已取得事实不等于 Agent 已消费提交；输出处理仍可能抛出。
                self._unknown = self._unknown or self._state.unknown_effects

    def publish(self, state: SessionContext, *, effects_observed: bool,
                journal_revision: int | None = None) -> None:
        with self._lock:
            if not self._closed:
                self._state = state
                self._has_publication = True
                self._agent_published = True
                self._effects_observed = effects_observed
                if effects_observed and journal_revision is not None:
                    self._consumed_revision = journal_revision
                self._unknown = self._unknown or state.unknown_effects

    @property
    def consumed_revision(self) -> int | None:
        with self._lock:
            return self._consumed_revision

    @property
    def unknown_effects(self) -> bool:
        with self._lock:
            return self._unknown

    def reconcile(self, original: SessionContext) -> tuple[SessionContext, bool]:
        with self._lock:
            state, observed, unknown = self._state, self._effects_observed, self._unknown
            published = self._has_publication
            agent_published = self._agent_published
        if state is not None and published:
            # 不恢复 Agent 的未交付消息草稿，只应用已消费的可信执行事实。
            original = replace(original,
                               # Registry 逐工具路径不是任务净变化；custom Agent 的路径由 T1 账本补齐。
                               modified_files=state.modified_files if agent_published else original.modified_files,
                               verification=state.verification, unknown_effects=state.unknown_effects,
                               verification_evidence=state.verification_evidence,
                               verification_failure=state.verification_failure,
                               verification_required=state.verification_required)
        elif state is not None:
            # 无新事实时保留兼容 Agent 的普通字段，但它不能移除已有验证约束。
            failure = state.verification_failure or original.verification_failure
            required = state.verification_required or original.verification_required
            if failure != original.verification_failure or required != original.verification_required:
                original = replace(original, verification_failure=failure, verification_required=required)
        if unknown:
            original = replace(original, unknown_effects=True, verification="待验证",
                               verification_evidence=None, verification_required=True)
        return original, state is not None and observed

    def close(self) -> None:
        with self._lock:
            # 迟到 worker 仍持有旧对象也不能写入下一任务；证据不跨任务残留。
            self._closed = True
            self._state = None
            self._has_publication = False
            self._agent_published = False
            self._consumed_revision = None


_current: ContextVar[TaskObservation | None] = ContextVar("tricoder_task_observation", default=None)


def current_task_observation() -> TaskObservation | None:
    return _current.get()


@contextmanager
def task_observation_scope():
    parent = _current.get()
    if parent is not None:
        yield parent
        return
    observation = TaskObservation()
    token = _current.set(observation)
    try:
        yield observation
    finally:
        observation.close()
        _current.reset(token)
