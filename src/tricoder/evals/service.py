"""Production assembly for the ``tricoder eval`` command."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.config import load_config
from tricoder.context.summarizer import MemorySummarizer
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
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore
from tricoder.tools import ToolContext, ToolRegistry

from .experiment import run_experiment
from .faults import ControlledFaults, FaultInjectingProvider, FaultInjectingTools
from .loader import MAX_EXPERIMENT_REPETITIONS, load_experiment, load_suite
from .models import (
    EvalCase,
    EvalCondition,
    ExperimentDefinition,
    PlannedTrial,
    ScenarioStep,
    TrialDimensions,
    TrialRecord,
)
from .output import reserve_run_directory
from .report import write_reports
from .runner import EvalCaseResult, run_isolated_case, run_suite
from .scenarios import combine_scenario_results, run_runtime_scenario


ProviderFactory = Callable[[ProviderConfig, float], ModelProvider]


@dataclass(slots=True)
class _ExecutionObservation:
    """只在单个 trial 内传递，不跨重复实验共享。"""

    summary_usage: TokenUsage | None = None
    summary_count: int = 0
    faults: ControlledFaults | None = None
    summary_usage_complete: bool = True

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
            if suite_path is not None or args.case is not None or repeat != 1:
                raise ValueError("experiment cannot be combined with suite filters")
            experiment = load_experiment(experiment_path)
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
        configs = condition_configs or {
            condition.id: load_config(
                provider=condition.provider,
                workspace=project_root,
                environ=environ,
                env_file=args.env_file,
                model=condition.model,
                base_url=None,
            )
            for condition in definition.conditions
        }
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
        return _trial_from_case_result(plan, case_result, observation=observation)

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

    database = (audit_path.parent / f"{case.id}-sessions.db").resolve()
    scripted_approvals: list[bool] = []

    def approver(_action: str, _detail: str) -> bool:
        return scripted_approvals.pop(0) if scripted_approvals else True

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
                )
            )
            provider: object = provider_factory(
                case_config.provider, case_config.timeout
            )
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
            workspace_confirmer=lambda _preview: True,
        )

    scenario = None
    try:
        steps = case.steps or (ScenarioStep("user_turn", content=case.task),)
        scenario = run_runtime_scenario(
            steps,
            runtime_factory,
            approval_decision_sink=scripted_approvals.append,
        )
        return combine_scenario_results(scenario.results)
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
    return TrialRecord(
        key=plan.key,
        execution_kind=plan.condition.execution_kind,
        status=result.status,
        dimensions=TrialDimensions(
            artifact_correct=(primary_result if "artifact_correct" in applicable else None),
            behavior_correct=(primary_result if "behavior_correct" in applicable else None),
            agent_completed=(
                None
                if result.status == "error"
                else "agent_failed" not in result.failure_codes
            ) if "agent_completed" in applicable else None,
            scope_compliant=(
                None
                if "workspace_error" in result.failure_codes
                else "change_out_of_scope" not in result.failure_codes
            ) if "scope_compliant" in applicable else None,
            cleanup_confirmed=(
                None if "workspace_error" in result.failure_codes else True
            ) if "cleanup_confirmed" in applicable else None,
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
                primary_result if "memory_constraint_preserved" in applicable else None
            ),
            memory_latest_correction_used=(
                primary_result if "memory_latest_correction_used" in applicable else None
            ),
            memory_stale_state_avoided=(
                primary_result if "memory_stale_state_avoided" in applicable else None
            ),
            memory_session_isolated=(
                primary_result if "memory_session_isolated" in applicable else None
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
