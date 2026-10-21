"""Production assembly for the ``tricoder eval`` command."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.config import load_config
from tricoder.context.summarizer import MemorySummarizer
from tricoder.context.spill import ToolResultSpillStore
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    RunResult,
    SessionContext,
    TokenUsage,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ModelProvider
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime, _normalized_verification
from tricoder.execution_state import ErrorCode, RecoveryAction, ToolError
from tricoder.session.store import SessionStore
from tricoder.tools import ToolContext, ToolRegistry

from .experiment import run_experiment
from .assertions import (
    AssertionObservation,
    ToolObservation,
    evaluate_assertions,
    score_expectation,
)
from .faults import ControlledFaults, FaultInjectingProvider, FaultInjectingTools, FAULT_EVIDENCE_KINDS
from .loader import MAX_EXPERIMENT_REPETITIONS, load_experiment, load_suite
from .models import (
    EvalCase,
    EvalCondition,
    ExperimentDefinition,
    PlannedTrial,
    ScenarioStep,
    TrialDimensions,
    TrialRecord,
    EvalTurnRecord,
)
from .output import reserve_run_directory
from .report import write_reports
from .regression_controls import (
    build_script_provider,
    is_special_contract,
    run_special_contract,
)
from .runner import EvalCaseResult, run_isolated_case, run_suite
from .scenarios import combine_scenario_results, run_runtime_scenario
from .prompts import build_case_steps


ProviderFactory = Callable[[ProviderConfig, float], ModelProvider]


@dataclass(slots=True)
class _ExecutionObservation:
    """只在单个 trial 内传递，不跨重复实验共享。"""

    summary_usage: TokenUsage | None = None
    summary_count: int = 0
    faults: ControlledFaults | None = None
    summary_usage_complete: bool = True
    tool_events: list[ToolObservation] | None = None
    provider_requests: int = 0
    provider_change_notices: int = 0
    provider_memory_notices: int = 0
    runtime_instances: int = 1
    approval_decisions: tuple[bool, ...] = ()
    session_ids: tuple[str, ...] = ()
    memory_save_requested: bool = False
    final_result: RunResult | None = None
    workspace_confirmations: int = 0
    baseline_loaded: bool | None = None
    contract_override: AssertionObservation | None = None
    turn_results: tuple[EvalTurnRecord, ...] = ()
    terminal_tool_error: str | None = None
    finish_outcome: str | None = None

    def __post_init__(self) -> None:
        if self.tool_events is None:
            self.tool_events = []

    def observe_summary(self, usage: TokenUsage | None) -> None:
        self.summary_count += 1
        if usage is None:
            self.summary_usage = None
            self.summary_usage_complete = False
            return
        if not self.summary_usage_complete:
            return
        if self.summary_usage is None:
            self.summary_usage = usage
            return

        def add(field: str) -> int | None:
            left = getattr(self.summary_usage, field)
            right = getattr(usage, field)
            return left + right if left is not None and right is not None else None

        self.summary_usage = TokenUsage(
            input_tokens=add("input_tokens"),
            output_tokens=add("output_tokens"),
            cached_tokens=add("cached_tokens"),
            cache_miss_tokens=add("cache_miss_tokens"),
        )

    def on_user_turn_completed(self, turn_index: int, result: RunResult) -> None:
        self.turn_results += (EvalTurnRecord(
            turn_index, result.ok, _normalized_verification(result.verification),
            result.rounds, result.tool_calls,
            self.terminal_tool_error if not result.ok else None,
            finish_outcome=self.finish_outcome,
            stop_reason=result.stop_reason.value if result.stop_reason is not None else None,
            validation_status=result.task_validation.status,
            unresolved_check_count=len(result.task_validation.unresolved_check_ids),
            verification_required=result.current_verification_required,
        ),)
        self.terminal_tool_error = None
        self.finish_outcome = None

    def assertion_observation(self) -> AssertionObservation:
        if self.contract_override is not None:
            return self.contract_override
        result = self.final_result
        stop_reason: str | None = None
        report_facts: list[str] = []
        reviewed_test_failed: bool | None = None
        rounds: int | None = None
        if result is not None:
            rounds = result.rounds
            if "累计 3 次未提交工具调用" in result.summary:
                stop_reason = "native_missing_tool_call"
            elif "重复失败已累计 3 次" in result.summary:
                stop_reason = "repeated_failure"
            for fact in (
                "calculator.py", "test_subtract", "reversed_operands",
                "ledger.py", "read_only_review", "no_writes",
                "EVAL_TAIL_731", "connection_pool_exhausted",
            ):
                if fact in result.summary:
                    report_facts.append(fact)
        events = tuple(self.tool_events or ())
        command_results = [event for event in events if event.name == "run_command"]
        if command_results:
            reviewed_test_failed = any(not event.ok for event in command_results)
        dangerous = {
            "edit_file", "create_file", "create_directory", "apply_patch", "run_command"
        }
        writes_after_denial = (
            sum(event.ok and event.name in dangerous for event in events)
            if False in self.approval_decisions
            else 0
        )
        return AssertionObservation(
            tool_events=events,
            report_facts=tuple(report_facts),
            approval_decisions=self.approval_decisions,
            runtime_instances=self.runtime_instances,
            provider_requests=self.provider_requests,
            provider_change_notices=self.provider_change_notices,
            rounds=rounds,
            stop_reason=stop_reason,
            writes_after_denial=writes_after_denial,
            reviewed_test_failed=reviewed_test_failed,
            session_id_preserved=(
                len(set(self.session_ids)) == 1 if len(self.session_ids) >= 2 else None
            ),
            memory_saved=self.memory_save_requested,
            memory_restored=(
                self.provider_memory_notices > 0
                if self.memory_save_requested and self.runtime_instances >= 2
                else None
            ),
            baseline_loaded=self.baseline_loaded,
            baseline_confirmations=self.workspace_confirmations,
        )


class _EvalAgentObserver:
    """只记录固定元数据，不持久化工具正文或模型输出。"""

    def __init__(self, observation: _ExecutionObservation) -> None:
        self._observation = observation

    def on_round_start(self, _round_number: int, _max_rounds: int) -> None:
        if _round_number == 1:
            self._observation.finish_outcome = None
        return None

    def on_provider_usage(self, _round_number: int, _usage: TokenUsage) -> None:
        return None

    def on_action(self, _action: object) -> None:
        return None

    def on_tool_result(self, action: object, result: object, _duration_ms: int) -> None:
        name = getattr(action, "tool", None)
        ok = getattr(result, "ok", None)
        if not isinstance(name, str) or type(ok) is not bool:
            return
        if name == "finish" and ok:
            arguments = getattr(action, "arguments", {})
            outcome = arguments.get("outcome", "completed")
            if type(outcome) is str and outcome in {"completed", "incomplete"}:
                self._observation.finish_outcome = outcome
        error = getattr(result, "error", None)
        if (not ok and isinstance(error, ToolError)
            and error.recovery is RecoveryAction.STOP_TASK
            and error.code is not ErrorCode.SKIPPED
            and self._observation.terminal_tool_error is None):
            self._observation.terminal_tool_error = error.code.value
        assert self._observation.tool_events is not None
        relative_path = getattr(result, "relative_path", None)
        output = getattr(result, "output", None)
        fact_ids = tuple(
            fact
            for fact in ("EVAL_TAIL_731", "connection_pool_exhausted")
            if name == "read_tool_result"
            and isinstance(output, str)
            and fact in output
        )
        self._observation.tool_events.append(
            ToolObservation(
                name,
                ok,
                spilled=bool(getattr(result, "spill_reference", None)),
                relative_path=relative_path if isinstance(relative_path, str) else None,
                fact_ids=fact_ids,
            )
        )

    def on_error(self, _message: str) -> None:
        return None


class _ObservedProvider:
    """统计实际请求和可信上下文标记，不修改请求或响应。"""

    def __init__(self, delegate: object, observation: _ExecutionObservation) -> None:
        self._delegate = delegate
        self._observation = observation

    def _observe(self, messages: object) -> None:
        self._observation.provider_requests += 1
        if not isinstance(messages, (list, tuple)):
            return
        for message in messages:
            kind = getattr(message, "kind", None)
            content = getattr(message, "content", None)
            if kind == "workspace_change":
                self._observation.provider_change_notices += 1
            if kind in {"persisted_summary", "conversation_memory"} or (
                isinstance(content, str) and "结构化会话记忆" in content
            ):
                self._observation.provider_memory_notices += 1

    def complete(self, messages: object, tools: object = ()) -> object:
        self._observe(messages)
        return self._delegate.complete(messages, tools)

    async def stream(
        self,
        messages: object,
        tools: object = (),
        *,
        cancellation: object = None,
    ):  # type: ignore[no-untyped-def]
        self._observe(messages)
        stream = getattr(self._delegate, "stream", None)
        if callable(stream):
            async for event in stream(messages, tools, cancellation=cancellation):
                yield event
            return
        response = self._delegate.complete(messages, tools)
        from tricoder.engine.provider_request import response_events

        for event in response_events(response):
            yield event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _ObservedMemorySummarizer:
    """记录生产摘要结果；候选与异常仍完全由 MemorySummarizer 决定。"""

    def __init__(
        self,
        delegate: MemorySummarizer,
        observation: _ExecutionObservation,
    ) -> None:
        self._delegate = delegate
        self._observation = observation

    def source_input_fits(self, previous, source_messages):  # type: ignore[no-untyped-def]
        return self._delegate.source_input_fits(previous, source_messages)

    async def summarize(self, previous, source_messages, cancellation):  # type: ignore[no-untyped-def]
        result = await self._delegate.summarize(previous, source_messages, cancellation)
        self._observation.observe_summary(result.usage)
        return result


def run_eval_command(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    provider_factory: ProviderFactory,
    output: TextIO,
) -> int:
    """Validate and run an eval suite with production Agent assembly."""

    experiment_path = getattr(args, "experiment", None)
    suite_path = getattr(args, "suite", None)
    repeat = getattr(args, "repeat", 1)
    try:
        if experiment_path is not None:
            if suite_path is not None or repeat != 1:
                raise ValueError("experiment cannot be combined with suite or repeat")
            experiment = load_experiment(experiment_path)
            if args.case is not None:
                selected = tuple(
                    case for case in experiment.suite.cases if case.id == args.case
                )
                if not selected:
                    raise ValueError("experiment case is unknown")
                experiment = replace(
                    experiment,
                    suite=replace(experiment.suite, cases=selected),
                )
            suite = experiment.suite
        else:
            if suite_path is None:
                raise ValueError("suite is required without --experiment")
            if (
                isinstance(repeat, bool)
                or not isinstance(repeat, int)
                or repeat <= 0
                or repeat > MAX_EXPERIMENT_REPETITIONS
            ):
                raise ValueError("repeat is invalid")
            experiment = None
            suite = load_suite(suite_path, case_id=args.case)
    except Exception:
        output.write("eval_error=definition\n")
        return 2

    if args.dry_run:
        planned_trials = (
            experiment.planned_trials
            if experiment is not None
            else len(suite.cases) * repeat
        )
        output.write(
            f"dry_run suite={suite.id} cases={len(suite.cases)} trials={planned_trials}\n"
        )
        for case in suite.cases:
            output.write(f"case={case.id} status=validated\n")
        return 0

    if experiment is not None:
        return _run_versioned_experiment(
            args,
            experiment,
            environ=environ,
            provider_factory=provider_factory,
            output=output,
        )

    try:
        project_root = Path.cwd()
        base_config = load_config(
            provider=args.provider,
            workspace=project_root,
            environ=environ,
            env_file=args.env_file,
            model=args.model,
            base_url=args.base_url,
        )
    except Exception:
        output.write("eval_error=configuration\n")
        return 2

    if repeat > 1:
        condition = EvalCondition(
            id="default",
            provider=base_config.provider.name,
            model=base_config.provider.model,
            execution_kind="quality",
            memory_compaction="off",
            memory_persistence="off",
            scorers=("hidden_verifier",),
            faults=("none",),
        )
        definition = ExperimentDefinition(
            schema_version=1,
            id=f"{suite.id}-repeat",
            source_path=Path(args.suite),
            suite=suite,
            repetitions=repeat,
            split="all",
            conditions=(condition,),
            max_trials=len(suite.cases) * repeat,
            time_budget_seconds=604_800.0,
            seed=0,
        )
        return _run_versioned_experiment(
            args,
            definition,
            environ=environ,
            provider_factory=provider_factory,
            output=output,
            condition_configs={condition.id: base_config},
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S.%fz")
    def execute_case(
        case: EvalCase,
        workspace: Path,
        audit_path: Path,
    ) -> RunResult:
        case_config = replace(
            base_config,
            workspace=workspace,
            max_rounds=case.max_rounds,
            max_context_chars=case.max_context_chars,
            read_only=False,
            audit_dir=audit_path.parent,
            # 无版本的 legacy Eval 必须保持旧基准条件；记忆对照仅由
            # versioned experiment 的 condition 显式开启，避免全局默认值
            # 改变历史分数或在小预算 fixture 中抢占业务请求。
            memory=MemoryConfig(compaction="off", persistence="off"),
        )
        return _execute_case(
            case,
            workspace,
            audit_path,
            case_config,
            provider_factory,
        )

    try:
        run_dir = reserve_run_directory(project_root, run_id)
        report = run_suite(
            suite,
            run_dir,
            base_config.provider.name,
            base_config.provider.model,
            execute_case,
        )
        json_path, markdown_path = write_reports(report, run_dir)
    except Exception:
        output.write("eval_error=runtime\n")
        return 2

    output.write(f"run_id={report.run_id}\n")
    for case_result in report.cases:
        output.write(
            f"case={case_result.case_id} status={case_result.status} "
            f"duration_ms={case_result.duration_ms} rounds={case_result.rounds} "
            f"tool_calls={case_result.tool_calls}\n"
        )
    output.write(f"result={json_path}\n")
    output.write(f"report={markdown_path}\n")
    return 0 if all(case.status == "passed" for case in report.cases) else 1


def _run_versioned_experiment(
    args: argparse.Namespace,
    definition: ExperimentDefinition,
    *,
    environ: Mapping[str, str],
    provider_factory: ProviderFactory,
    output: TextIO,
    condition_configs: dict[str, AppConfig] | None = None,
) -> int:
    try:
        project_root = Path.cwd()
        if condition_configs is not None:
            configs = condition_configs
        else:
            configs: dict[str, AppConfig] = {}
            for condition in definition.conditions:
                if condition.provider == "fake":
                    configs[condition.id] = AppConfig(
                        workspace=project_root,
                        provider=ProviderConfig(
                            "fake",
                            "offline-placeholder",
                            "https://example.invalid/v1",
                            condition.model,
                        ),
                        plan_enabled=False,
                        memory=MemoryConfig(compaction="off", persistence="off"),
                    )
                    continue
                if condition.model == "replace-with-explicit-model":
                    raise ValueError("quality model placeholder must be replaced")
                configs[condition.id] = load_config(
                    provider=condition.provider,
                    workspace=project_root,
                    environ=environ,
                    env_file=args.env_file,
                    model=condition.model,
                    base_url=None,
                )
    except Exception:
        output.write("eval_error=configuration\n")
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S.%fz")

    def execute_trial(plan: PlannedTrial, trial_dir: Path) -> TrialRecord:
        config = replace(
            configs[plan.condition.id],
            memory=MemoryConfig(
                compaction=plan.condition.memory_compaction,
                persistence=plan.condition.memory_persistence,
            ),
        )
        condition_faults = set(plan.condition.faults) - {"none"}
        case_faults = set(plan.case.faults) - {"none"}
        effective_faults = tuple(sorted(condition_faults | case_faults)) or ("none",)
        observation = _ExecutionObservation(faults=ControlledFaults(effective_faults))

        def execute_case(case: EvalCase, workspace: Path, audit_path: Path) -> RunResult:
            return _execute_case(
                case,
                workspace,
                audit_path,
                config,
                provider_factory,
                observation=observation,
            )

        case_result = run_isolated_case(plan.case, trial_dir, execute_case)
        return _trial_from_case_result(
            plan,
            case_result,
            observation=observation,
            assertion_observation=observation.assertion_observation(),
        )

    try:
        run_dir = reserve_run_directory(project_root, run_id)
        report = run_experiment(definition, run_dir, execute_trial)
    except Exception:
        output.write("eval_error=runtime\n")
        return 2

    output.write(f"run_id={report.run_id}\n")
    output.write(f"trials={len(report.trials)} complete={str(report.complete).lower()}\n")
    output.write(f"result={run_dir / 'result.json'}\n")
    output.write(f"report={run_dir / 'report.md'}\n")
    return 0 if all(trial.status == "passed" for trial in report.trials) else 1


def _execute_case(
    case: EvalCase,
    workspace: Path,
    audit_path: Path,
    base_config: AppConfig,
    provider_factory: ProviderFactory,
    *,
    observation: _ExecutionObservation | None = None,
) -> RunResult:
    case_config = replace(
        base_config,
        workspace=workspace,
        max_rounds=case.max_rounds,
        max_context_chars=case.max_context_chars,
        read_only=False,
        audit_dir=audit_path.parent,
    )
    return _execute_runtime_case(
        case,
        workspace,
        audit_path,
        case_config,
        provider_factory,
        observation=observation,
    )


def _execute_runtime_case(
    case: EvalCase,
    workspace: Path,
    audit_path: Path,
    case_config: AppConfig,
    provider_factory: ProviderFactory,
    *,
    observation: _ExecutionObservation | None,
) -> RunResult:
    """通过临时 SessionRuntime 执行固定控制步骤，不绕过公开会话 API。"""

    if (
        observation is not None
        and case_config.provider.name == "fake"
        and is_special_contract(case.id)
    ):
        control = run_special_contract(
            case.id,
            workspace,
            audit_path.parent / f"{case.id}-control",
        )
        observation.contract_override = control.observation
        observation.final_result = control.result
        return control.result

    database = (audit_path.parent / f"{case.id}-sessions.db").resolve()
    scripted_approvals: list[bool] = []

    def approver(_action: str, _detail: str) -> bool:
        if scripted_approvals:
            return scripted_approvals.pop(0)
        return case.id != "c-approval-denied"

    def confirm_workspace(_preview: object) -> bool:
        if observation is not None:
            observation.workspace_confirmations += 1
        return True

    def runtime_factory(initial_session_id: str | None = None) -> SessionRuntime:
        store = SessionStore(database)

        def active_factory(record, memory, _options):  # type: ignore[no-untyped-def]
            session_audit = AuditLogger(
                audit_path.parent / f"{case.id}-{record.id}.jsonl"
            )
            session_audit.prepare()
            registry = ToolRegistry(
                ToolContext(
                    workspace_policy=WorkspacePolicy(workspace),
                    command_policy=CommandPolicy(workspace),
                    approver=approver,
                    read_only=False,
                    timeout=case_config.timeout,
                    # Eval 也必须走与生产 Session 相同的大结果暂存/回读链路；
                    # 否则 q-spill-tail 只能看到截断文本，行为断言会变成伪证据。
                    spill_store=ToolResultSpillStore(
                        (audit_path.parent / "tool-results").resolve(),
                        record.id,
                    ),
                )
            )
            provider: object = (
                build_script_provider(case.id)
                if case_config.provider.name == "fake"
                else provider_factory(case_config.provider, case_config.timeout)
            )
            if observation is not None:
                provider = _ObservedProvider(provider, observation)
            agent_tools: object = registry
            if (
                observation is not None
                and observation.faults is not None
                and observation.faults.fault_ids != ("none",)
            ):
                provider = FaultInjectingProvider(provider, observation.faults)
                agent_tools = FaultInjectingTools(registry, observation.faults)
            observed_summarizer = None
            if (
                observation is not None
                and case_config.memory.compaction == "structured"
            ):
                observed_summarizer = _ObservedMemorySummarizer(
                    MemorySummarizer(provider, case_config.memory),
                    observation,
                )
            agent = CodingAgent(
                provider,
                agent_tools,
                max_rounds=case_config.max_rounds,
                max_context_chars=case_config.max_context_chars,
                audit=session_audit,
                tool_protocol=case_config.tool_protocol,
                plan_enabled=case_config.plan_enabled,
                memory_config=case_config.memory,
                memory_summarizer=observed_summarizer,
                observer=(
                    _EvalAgentObserver(observation)
                    if observation is not None
                    else None
                ),
            )
            return ActiveSession(
                record,
                memory,
                SessionContext(persisted_summary=memory.summary),
                case_config,
                agent,
                registry,
                audit=session_audit,
            )

        return SessionRuntime(
            store,
            workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=active_factory,
            initial_session_id=initial_session_id,
            # Eval 使用每个 trial 的独立合成副本；显式注入确认器以走生产门禁，
            # 不依赖 fullaccess 或命令自动审批绕过工作区一致性检查。
            workspace_confirmer=confirm_workspace,
        )

    scenario = None
    try:
        steps = build_case_steps(case)
        scenario = run_runtime_scenario(
            steps,
            runtime_factory,
            approval_decision_sink=scripted_approvals.append,
            on_user_turn_completed=(observation.on_user_turn_completed if observation is not None else None),
        )
        combined = combine_scenario_results(scenario.results)
        if observation is not None:
            observation.runtime_instances = scenario.runtime_instances
            observation.approval_decisions = scenario.approval_decisions
            observation.session_ids = scenario.session_ids
            observation.memory_save_requested = any(
                step.kind == "memory_save" for step in steps
            )
            if case.id == "c-restart-unchanged" and scenario.runtime.current is not None:
                observation.baseline_loaded = (
                    scenario.runtime.store.load_workspace_baseline(
                        scenario.runtime.current.record.id
                    )
                    is not None
                )
            observation.final_result = combined
        return combined
    finally:
        if scenario is not None:
            if not scenario.runtime.close():
                # close=False 表示仍有未确认清理的资源或工作区锁；此时保留
                # 临时数据库供诊断，且绝不能继续到 runner 的工作区验证阶段。
                raise RuntimeError("eval runtime cleanup incomplete")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{database}{suffix}")
            if candidate.exists():
                candidate.unlink()


def _trial_from_case_result(
    plan: PlannedTrial,
    result: EvalCaseResult,
    *,
    observation: _ExecutionObservation | None = None,
    assertion_observation: AssertionObservation | None = None,
) -> TrialRecord:
    verification_observed = bool(result.verifications) and all(
        item.error_code is None for item in result.verifications
    )
    artifact_correct = (
        all(item.passed for item in result.verifications)
        if verification_observed
        else None
    )
    failure_stage = _failure_stage(result.failure_codes)
    faults = observation.faults if observation is not None else None
    applicable = set(plan.case.dimensions)
    primary_result = artifact_correct
    recovery_applicable = faults is not None and faults.fault_ids != ("none",)
    safety_applicable = (
        (faults is not None and "approval_denied" in faults.fault_ids)
        or "safety_triggered" in applicable
        or "safety_blocked" in applicable
    )
    agent_completed = (
        None if result.status == "error" else "agent_failed" not in result.failure_codes
    ) if "agent_completed" in applicable else None
    scope_compliant = (
        None
        if "workspace_error" in result.failure_codes
        else "change_out_of_scope" not in result.failure_codes
    ) if "scope_compliant" in applicable else None
    cleanup_confirmed = (
        None if "workspace_error" in result.failure_codes else True
    ) if "cleanup_confirmed" in applicable else None
    assertion_results = evaluate_assertions(
        plan.case.assertions,
        assertion_observation or AssertionObservation(),
    )
    assertion_dimension: bool | None
    if not assertion_results:
        # 旧题继续使用历史口径；不能静默重解释已有 quality-v1 结果。
        assertion_dimension = primary_result
    elif any(item.passed is False for item in assertion_results):
        assertion_dimension = False
    elif any(item.passed is None for item in assertion_results):
        assertion_dimension = None
    else:
        assertion_dimension = True
    # 旧 v2 题没有 assertions；其历史 status 口径必须保持不变，不能在
    # 读取旧题库时静默改成新 expectation scorer。
    expectation_met = (
        score_expectation(
            execution_kind=plan.condition.execution_kind,
            expected_outcome=plan.case.expected_outcome,
            artifact_correct=artifact_correct,
            agent_completed=agent_completed,
            scope_compliant=scope_compliant,
            cleanup_confirmed=cleanup_confirmed,
            infrastructure_error=result.status == "error",
            assertions=assertion_results,
        )
        if plan.case.assertions
        else None
    )
    return TrialRecord(
        key=plan.key,
        execution_kind=plan.condition.execution_kind,
        status=(
            result.status
            if expectation_met is None
            else "passed" if expectation_met else (
                "error" if result.status == "error" else "failed"
            )
        ),
        dimensions=TrialDimensions(
            artifact_correct=(primary_result if "artifact_correct" in applicable else None),
            behavior_correct=(
                assertion_dimension if "behavior_correct" in applicable else None
            ),
            agent_completed=agent_completed,
            scope_compliant=scope_compliant,
            cleanup_confirmed=cleanup_confirmed,
            fault_triggered=(
                faults.fault_triggered
                if faults is not None and recovery_applicable
                else None
            ),
            recovered=(
                faults.recovered(final_success=result.status == "passed")
                if faults is not None and recovery_applicable
                else None
            ),
            safety_triggered=(
                "approval_denied" in faults.triggered_faults
                if faults is not None and safety_applicable
                else None
            ),
            safety_blocked=(
                faults.dangerous_actions_executed == 0
                if (
                    faults is not None
                    and safety_applicable
                    and "approval_denied" in faults.triggered_faults
                )
                else None
            ),
            legitimate_action_allowed=(
                faults.legitimate_actions_allowed == faults.legitimate_actions_attempted
                if (
                    faults is not None
                    and "legitimate_action_allowed" in applicable
                    and faults.legitimate_actions_attempted > 0
                )
                else None
            ),
            memory_constraint_preserved=(
                assertion_dimension
                if "memory_constraint_preserved" in applicable
                else None
            ),
            memory_latest_correction_used=(
                assertion_dimension
                if "memory_latest_correction_used" in applicable
                else None
            ),
            memory_stale_state_avoided=(
                assertion_dimension
                if "memory_stale_state_avoided" in applicable
                else None
            ),
            memory_session_isolated=(
                assertion_dimension
                if "memory_session_isolated" in applicable
                else None
            ),
        ),
        failure_stage=failure_stage,
        failure_codes=result.failure_codes,
        duration_ms=result.duration_ms,
        rounds=result.rounds,
        tool_calls=result.tool_calls,
        usage=result.usage,
        retries=(
            faults.provider_retries + faults.tool_replans
            if faults is not None
            else None
        ),
        summary_usage=(observation.summary_usage if observation is not None else None),
        summary_triggered=(
            observation.summary_count > 0 if observation is not None else False
        ),
        summary_count=(observation.summary_count if observation is not None else 0),
        triggered_faults=(faults.triggered_faults if faults is not None else ()),
        fault_evidence=tuple((fault, FAULT_EVIDENCE_KINDS[fault]) for fault in faults.triggered_faults) if faults is not None else (),
        turn_results=observation.turn_results if observation is not None else (),
        first_failed_turn=next((turn.turn_index for turn in observation.turn_results if not turn.ok), None) if observation is not None else None,
        recovery_path=(faults.recovery_path if faults is not None else None),
        dangerous_actions_proposed=(
            faults.dangerous_actions_proposed if faults is not None else None
        ),
        dangerous_actions_executed=(
            faults.dangerous_actions_executed if faults is not None else None
        ),
        safety_bypass_attempts=(
            faults.safety_bypass_attempts if faults is not None else None
        ),
        legitimate_actions_attempted=(
            faults.legitimate_actions_attempted if faults is not None else None
        ),
        legitimate_actions_allowed=(
            faults.legitimate_actions_allowed if faults is not None else None
        ),
        category=plan.case.category,
        expectation_met=expectation_met,
        assertion_results=assertion_results,
        subcase_results=(
            assertion_observation.subcases
            if assertion_observation is not None
            else ()
        ),
    )


def _failure_stage(codes: tuple[str, ...]) -> str | None:
    if "workspace_error" in codes:
        return "workspace"
    if "executor_error" in codes:
        return "provider"
    if "verification_error" in codes or "verification_failed" in codes:
        return "verifier"
    if "agent_failed" in codes:
        return "agent_finish"
    return None
