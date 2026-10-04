"""Fixed multi-turn and SessionRuntime scenario orchestration for Eval."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tricoder.models import RunResult, SessionContext, TokenUsage
from tricoder.session.runtime import SessionRuntime

from .models import ScenarioStep


@dataclass(frozen=True, slots=True)
class AgentTurnObservation:
    result: RunResult
    context: SessionContext
    memory_usage: TokenUsage | None
    memory_calls: int


@dataclass(frozen=True, slots=True)
class RuntimeScenarioObservation:
    runtime: SessionRuntime
    results: tuple[RunResult, ...]
    runtime_instances: int
    approval_decisions: tuple[bool, ...]


def run_agent_turns(
    agent: object,
    turns: tuple[str, ...],
    *,
    context: SessionContext | None = None,
) -> AgentTurnObservation:
    """Run user turns on one production Agent context; controls never enter here."""

    if not turns or any(not isinstance(turn, str) or not turn.strip() for turn in turns):
        raise ValueError("multi-turn scenario requires non-empty user turns")
    current = context or SessionContext()
    results: list[RunResult] = []
    memory_usages: list[TokenUsage | None] = []
    memory_calls = 0
    run_with_context = getattr(agent, "run_with_context", None)
    if not callable(run_with_context):
        raise TypeError("scenario Agent must implement run_with_context")
    for turn in turns:
        outcome = run_with_context(turn, current)
        current = outcome.context
        results.append(outcome.result)
        memory_usages.append(outcome.memory_usage)
        memory_calls += outcome.memory_calls
        if not outcome.result.ok:
            break
    combined = _combine_results(tuple(results))
    return AgentTurnObservation(
        result=combined,
        context=current,
        memory_usage=_strict_usage(memory_usages, ignore_absent=True),
        memory_calls=memory_calls,
    )


def run_runtime_scenario(
    steps: tuple[ScenarioStep, ...],
    runtime_factory: Callable[[str | None], SessionRuntime],
    *,
    approval_decision_sink: Callable[[bool], None] | None = None,
) -> RuntimeScenarioObservation:
    """Execute only registered control actions through SessionRuntime public APIs."""

    if not steps:
        raise ValueError("runtime scenario must contain at least one step")
    runtime = runtime_factory(None)
    instances = 1
    results: list[RunResult] = []
    approval_decisions: list[bool] = []
    try:
        for step in steps:
            if step.kind == "user_turn":
                assert step.content is not None
                results.append(runtime.run_task(step.content))
            elif step.kind == "memory_refresh":
                runtime.refresh_memory()
            elif step.kind == "memory_save":
                if runtime.current.config.memory.persistence == "reviewed_summary":
                    preview = runtime.preview_memory_save()
                    runtime.save_memory_preview(preview)
            elif step.kind == "restart_session":
                if runtime.current is None:
                    raise RuntimeError("runtime scenario has no Session to restart")
                session_id = runtime.current.record.id
                if not runtime.close():
                    raise RuntimeError("runtime scenario could not close the previous session")
                runtime = runtime_factory(session_id)
                instances += 1
            elif step.kind == "switch_session":
                assert step.target is not None
                records = runtime.store.list_all()
                exact = [record for record in records if record.id == step.target]
                named = [
                    record
                    for record in records
                    if record.name == step.target
                ]
                if exact:
                    runtime.switch(exact[0].id, confirm=lambda _workspace: True)
                elif len(named) == 1:
                    runtime.switch(named[0].id, confirm=lambda _workspace: True)
                elif len(named) > 1:
                    raise ValueError(
                        "switch_session 名称目标存在歧义，请使用完整 Session ID"
                    )
                else:
                    runtime.create(step.target)
            elif step.kind == "undo":
                runtime.prepare_undo()
                runtime.undo_latest()
            elif step.kind == "approve":
                approval_decisions.append(True)
                if approval_decision_sink is not None:
                    approval_decision_sink(True)
            elif step.kind == "deny":
                approval_decisions.append(False)
                if approval_decision_sink is not None:
                    approval_decision_sink(False)
            else:  # pragma: no cover - ScenarioStep validates the enum.
                raise ValueError("unsupported runtime scenario step")
        return RuntimeScenarioObservation(
            runtime=runtime,
            results=tuple(results),
            runtime_instances=instances,
            approval_decisions=tuple(approval_decisions),
        )
    except BaseException as exc:
        if not runtime.close():
            raise RuntimeError("runtime scenario cleanup incomplete") from exc
        raise


def combine_scenario_results(results: tuple[RunResult, ...]) -> RunResult:
    """把多轮 Runtime 结果合并为 runner 需要的单个受信结果。"""

    return _combine_results(results)


def _combine_results(results: tuple[RunResult, ...]) -> RunResult:
    if not results:
        raise ValueError("scenario produced no Agent result")
    modified = tuple(dict.fromkeys(path for result in results for path in result.modified_files))
    return RunResult(
        ok=all(result.ok for result in results),
        summary=results[-1].summary,
        rounds=sum(result.rounds for result in results),
        tool_calls=sum(result.tool_calls for result in results),
        modified_files=modified,
        verification=results[-1].verification,
        usage=_strict_usage([result.usage for result in results], ignore_absent=False),
        unknown_effects=any(result.unknown_effects for result in results),
        cleanup_failed=any(result.cleanup_failed for result in results),
    )


def _strict_usage(
    usages: list[TokenUsage | None],
    *,
    ignore_absent: bool,
) -> TokenUsage | None:
    observed = [usage for usage in usages if usage is not None]
    if not observed:
        return None
    if not ignore_absent and len(observed) != len(usages):
        return None

    def total(field: str) -> int | None:
        values = [getattr(usage, field) for usage in observed]
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    return TokenUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        cached_tokens=total("cached_tokens"),
        cache_miss_tokens=total("cache_miss_tokens"),
    )
