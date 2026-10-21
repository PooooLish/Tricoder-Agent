"""固定回归题的宿主观察与注册断言。

这里故意不接受表达式、脚本或插件。题目只能引用仓库内注册的断言 ID，
模型文本也不能直接创建可信行为证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, TypeAlias

from .models import ExecutionKind


ExpectedOutcome: TypeAlias = Literal["completed", "blocked"]


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """一次由 Agent observer 看到的真实工具结果。"""

    name: str
    ok: bool
    spilled: bool = False
    relative_path: str | None = None
    # 仅记录仓库固定合成题的事实 ID，不保留工具原文或用户数据。
    fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubcaseResult:
    """复合契约题的独立结果，保留原始 Agent 完成事实。"""

    id: str
    expectation_met: bool
    agent_completed: bool
    reason_code: str = "ok"


@dataclass(frozen=True, slots=True)
class AssertionObservation:
    """单个 trial 的有界宿主事实；所有字段均由执行器填写。"""

    tool_events: tuple[ToolObservation, ...] = ()
    report_facts: tuple[str, ...] = ()
    approval_decisions: tuple[bool, ...] = ()
    runtime_instances: int | None = None
    provider_requests: int | None = None
    provider_change_notices: int | None = None
    rounds: int | None = None
    stop_reason: str | None = None
    writes_after_denial: int | None = None
    reviewed_test_failed: bool | None = None
    session_id_preserved: bool | None = None
    memory_saved: bool | None = None
    memory_restored: bool | None = None
    baseline_loaded: bool | None = None
    baseline_confirmations: int | None = None
    baseline_revision_preserved: bool | None = None
    corrupted_subcases: tuple[str, ...] = ()
    subcases: tuple[SubcaseResult, ...] = ()


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """一个固定断言的脱敏结果；``None`` 表示缺少宿主证据。"""

    id: str
    passed: bool | None
    reason_code: str
    evidence_refs: tuple[str, ...] = ()


AssertionEvaluator: TypeAlias = Callable[[AssertionObservation], AssertionResult]


def _result(
    assertion_id: str,
    passed: bool | None,
    reason: str,
    *refs: str,
) -> AssertionResult:
    return AssertionResult(assertion_id, passed, reason, tuple(refs))


def _readback_after_spill(observation: AssertionObservation) -> AssertionResult:
    spill_index = next(
        (index for index, event in enumerate(observation.tool_events) if event.spilled),
        None,
    )
    if spill_index is None:
        return _result("readback_after_spill", None, "spill_not_observed")
    required_facts = {"EVAL_TAIL_731", "connection_pool_exhausted"}
    readback = any(
        event.name == "read_tool_result"
        and event.ok
        and required_facts.issubset(event.fact_ids)
        for event in observation.tool_events[spill_index + 1 :]
    )
    return _result(
        "readback_after_spill",
        readback,
        "ok" if readback else "tail_readback_missing",
        "tool.spill",
        *("tool.read_tool_result",) if readback else (),
    )


def _review_report_facts(observation: AssertionObservation) -> AssertionResult:
    required = frozenset({"calculator.py", "test_subtract", "reversed_operands"})
    if observation.reviewed_test_failed is None and not observation.report_facts:
        return _result("review_report_facts", None, "observation_missing")
    passed = required.issubset(observation.report_facts) and (
        observation.reviewed_test_failed is True
    )
    return _result(
        "review_report_facts",
        passed,
        "ok" if passed else "report_fact_missing",
        "report.fixed-rubric",
    )


def _history_recap_facts(observation: AssertionObservation) -> AssertionResult:
    required = frozenset({"ledger.py", "read_only_review", "no_writes"})
    if not observation.report_facts or observation.provider_requests is None:
        return _result("history_recap_facts", None, "observation_missing")
    passed = (
        required.issubset(observation.report_facts)
        and observation.provider_requests >= 2
    )
    return _result(
        "history_recap_facts",
        passed,
        "ok" if passed else "recap_fact_missing",
        "turn.second",
    )


def _spill_tail_facts(observation: AssertionObservation) -> AssertionResult:
    required = frozenset({"EVAL_TAIL_731", "connection_pool_exhausted"})
    if not observation.report_facts:
        return _result("spill_tail_facts", None, "observation_missing")
    passed = required.issubset(observation.report_facts)
    return _result(
        "spill_tail_facts",
        passed,
        "ok" if passed else "tail_fact_missing",
        "report.spill-tail",
    )


def _runtime_restarted(observation: AssertionObservation) -> AssertionResult:
    if observation.runtime_instances is None:
        return _result("runtime_restarted", None, "observation_missing")
    passed = observation.runtime_instances >= 2
    return _result("runtime_restarted", passed, "ok" if passed else "restart_missing")


def _session_id_preserved(observation: AssertionObservation) -> AssertionResult:
    value = observation.session_id_preserved
    return _result(
        "session_id_preserved",
        value,
        "observation_missing" if value is None else ("ok" if value else "session_changed"),
    )


def _memory_restored(observation: AssertionObservation) -> AssertionResult:
    if observation.memory_saved is None or observation.memory_restored is None:
        return _result("memory_restored", None, "observation_missing")
    passed = observation.memory_saved and observation.memory_restored
    return _result("memory_restored", passed, "ok" if passed else "memory_not_restored")


def _approval_denied(observation: AssertionObservation) -> AssertionResult:
    if observation.writes_after_denial is None:
        return _result("approval_denied", None, "observation_missing")
    denied = False in observation.approval_decisions
    passed = denied and observation.writes_after_denial == 0
    reason = "ok" if passed else ("denial_missing" if not denied else "write_after_denial")
    return _result("approval_denied", passed, reason, *("approval.denied",) if denied else ())


def _provider_not_called(observation: AssertionObservation) -> AssertionResult:
    if observation.provider_requests is None:
        return _result("provider_not_called", None, "observation_missing")
    passed = observation.provider_requests == 0
    return _result("provider_not_called", passed, "ok" if passed else "provider_called")


def _provider_called(observation: AssertionObservation) -> AssertionResult:
    if observation.provider_requests is None:
        return _result("provider_called", None, "observation_missing")
    passed = observation.provider_requests > 0
    return _result("provider_called", passed, "ok" if passed else "provider_not_called")


def _workspace_change_notice(observation: AssertionObservation) -> AssertionResult:
    if observation.provider_change_notices is None:
        return _result("workspace_change_notice", None, "observation_missing")
    passed = observation.provider_change_notices == 1
    return _result(
        "workspace_change_notice",
        passed,
        "ok" if passed else "notice_count_invalid",
    )


def _text_termination_limit(observation: AssertionObservation) -> AssertionResult:
    if observation.stop_reason is None or observation.rounds is None:
        return _result("text_termination_limit", None, "observation_missing")
    passed = observation.stop_reason == "native_missing_tool_call" and observation.rounds == 3
    return _result(
        "text_termination_limit", passed, "ok" if passed else "wrong_stop_reason"
    )


def _repeat_failure_limit(observation: AssertionObservation) -> AssertionResult:
    if observation.stop_reason is None:
        return _result("repeat_failure_limit", None, "observation_missing")
    passed = observation.stop_reason == "repeated_failure"
    return _result(
        "repeat_failure_limit", passed, "ok" if passed else "wrong_stop_reason"
    )


def _baseline_reused(observation: AssertionObservation) -> AssertionResult:
    if observation.baseline_loaded is None or observation.baseline_confirmations is None:
        return _result("baseline_reused", None, "observation_missing")
    passed = observation.baseline_loaded and observation.baseline_confirmations == 0
    return _result("baseline_reused", passed, "ok" if passed else "baseline_not_reused")


def _baseline_changed_accept(observation: AssertionObservation) -> AssertionResult:
    values = (
        observation.baseline_loaded,
        observation.baseline_revision_preserved,
    )
    if any(value is None for value in values) or observation.baseline_confirmations is None:
        return _result("baseline_changed_accept", None, "observation_missing")
    passed = (
        observation.baseline_loaded is True
        and observation.baseline_confirmations == 1
        and observation.provider_change_notices == 1
        and observation.provider_requests is not None
        and observation.provider_requests > 0
    )
    return _result(
        "baseline_changed_accept", passed, "ok" if passed else "accept_flow_incomplete"
    )


def _baseline_changed_deny(observation: AssertionObservation) -> AssertionResult:
    if (
        observation.baseline_confirmations is None
        or observation.provider_requests is None
        or observation.baseline_revision_preserved is None
    ):
        return _result("baseline_changed_deny", None, "observation_missing")
    passed = (
        observation.baseline_confirmations == 1
        and observation.provider_requests == 0
        and observation.baseline_revision_preserved
    )
    return _result(
        "baseline_changed_deny", passed, "ok" if passed else "deny_flow_incomplete"
    )


def _baseline_changed_both(observation: AssertionObservation) -> AssertionResult:
    if not observation.subcases:
        return _result("baseline_changed_both", None, "observation_missing")
    by_id = {item.id: item for item in observation.subcases}
    accept = by_id.get("accept")
    deny = by_id.get("deny")
    passed = bool(
        accept is not None
        and deny is not None
        and accept.expectation_met
        and accept.agent_completed
        and deny.expectation_met
        and not deny.agent_completed
    )
    return _result(
        "baseline_changed_both", passed, "ok" if passed else "subcase_failed"
    )


def _baseline_corrupted_all(observation: AssertionObservation) -> AssertionResult:
    required = frozenset({"kind-array", "kind-object", "invalid-json"})
    if not observation.corrupted_subcases:
        return _result("baseline_corrupted_all", None, "observation_missing")
    passed = required == frozenset(observation.corrupted_subcases)
    return _result(
        "baseline_corrupted_all", passed, "ok" if passed else "subcase_missing"
    )


ASSERTION_REGISTRY: dict[str, AssertionEvaluator] = {
    "approval_denied": _approval_denied,
    "baseline_changed_accept": _baseline_changed_accept,
    "baseline_changed_both": _baseline_changed_both,
    "baseline_changed_deny": _baseline_changed_deny,
    "baseline_corrupted_all": _baseline_corrupted_all,
    "baseline_reused": _baseline_reused,
    "history_recap_facts": _history_recap_facts,
    "memory_restored": _memory_restored,
    "provider_called": _provider_called,
    "provider_not_called": _provider_not_called,
    "readback_after_spill": _readback_after_spill,
    "repeat_failure_limit": _repeat_failure_limit,
    "review_report_facts": _review_report_facts,
    "runtime_restarted": _runtime_restarted,
    "session_id_preserved": _session_id_preserved,
    "spill_tail_facts": _spill_tail_facts,
    "text_termination_limit": _text_termination_limit,
    "workspace_change_notice": _workspace_change_notice,
}
REGISTERED_ASSERTIONS = frozenset(ASSERTION_REGISTRY)


def evaluate_assertions(
    assertion_ids: tuple[str, ...],
    observation: AssertionObservation,
) -> tuple[AssertionResult, ...]:
    """Evaluate only fixed repository assertions in declaration order."""

    results: list[AssertionResult] = []
    for assertion_id in assertion_ids:
        try:
            evaluator = ASSERTION_REGISTRY[assertion_id]
        except KeyError as exc:
            raise ValueError(f"unregistered assertion: {assertion_id}") from exc
        results.append(evaluator(observation))
    return tuple(results)


def score_expectation(
    *,
    execution_kind: ExecutionKind,
    expected_outcome: ExpectedOutcome,
    artifact_correct: bool | None,
    agent_completed: bool | None,
    scope_compliant: bool | None,
    cleanup_confirmed: bool | None,
    infrastructure_error: bool,
    assertions: tuple[AssertionResult, ...],
) -> bool:
    """Score the declared outcome without rewriting original Agent facts."""

    if execution_kind not in {"quality", "contract"}:
        raise ValueError("unsupported execution kind")
    if expected_outcome not in {"completed", "blocked"}:
        raise ValueError("unsupported expected outcome")
    if infrastructure_error:
        return False
    if artifact_correct is not True:
        return False
    if scope_compliant is not True or cleanup_confirmed is not True:
        return False
    if any(result.passed is not True for result in assertions):
        return False
    if expected_outcome == "completed":
        return agent_completed is True
    return execution_kind == "contract" and agent_completed is False
