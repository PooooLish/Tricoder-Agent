"""Immutable domain objects for deterministic eval suites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from tricoder.models import TokenUsage
from tricoder.execution_state import ErrorCode, TaskStopReason

if TYPE_CHECKING:
    from .assertions import AssertionResult, SubcaseResult


ExecutionKind: TypeAlias = Literal["quality", "contract"]
ExpectedOutcome: TypeAlias = Literal["completed", "blocked"]
PathMatchMode: TypeAlias = Literal["legacy", "workspace_root"]
TrialStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "error",
    "cancelled",
    "not_run_budget",
    "not_run_cancelled",
]
ScenarioStepKind: TypeAlias = Literal[
    "user_turn",
    "memory_save",
    "memory_refresh",
    "restart_session",
    "switch_session",
    "approve",
    "deny",
    "undo",
]


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    name: str
    command: str
    timeout: float


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    title: str
    task: str
    source_dir: Path
    workspace_dir: Path
    verifier_dir: Path
    allowed_changes: tuple[str, ...]
    required_changes: tuple[str, ...]
    max_rounds: int
    max_context_chars: int
    verifications: tuple[VerificationSpec, ...]
    category: str = "coding"
    split: str = "dev"
    execution_kind: ExecutionKind = "quality"
    scorers: tuple[str, ...] = ("hidden_verifier",)
    faults: tuple[str, ...] = ("none",)
    dimensions: tuple[str, ...] = (
        "artifact_correct",
        "agent_completed",
        "scope_compliant",
        "cleanup_confirmed",
    )
    steps: tuple["ScenarioStep", ...] = ()
    assertions: tuple[str, ...] = ()
    expected_outcome: ExpectedOutcome = "completed"
    expose_edit_scope: bool = False
    path_match_mode: PathMatchMode = "legacy"


@dataclass(frozen=True, slots=True)
class EvalSuite:
    id: str
    title: str
    source_dir: Path
    cases: tuple[EvalCase, ...]
    schema_version: int = 1
    benchmark_version: str = "legacy"


@dataclass(frozen=True, slots=True)
class EvalCondition:
    """One controlled experiment condition with no credential-bearing fields."""

    id: str
    provider: str
    model: str
    execution_kind: ExecutionKind
    memory_compaction: str
    memory_persistence: str
    scorers: tuple[str, ...]
    faults: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    """Validated, bounded trial matrix loaded without constructing a Provider."""

    schema_version: int
    id: str
    source_path: Path
    suite: EvalSuite
    repetitions: int
    split: str
    conditions: tuple[EvalCondition, ...]
    max_trials: int
    time_budget_seconds: float
    seed: int

    @property
    def planned_trials(self) -> int:
        if self.suite.schema_version == 1:
            pairs = len(self.suite.cases) * len(self.conditions)
        else:
            pairs = sum(
                case.execution_kind == condition.execution_kind
                for case in self.suite.cases
                for condition in self.conditions
            )
        return pairs * self.repetitions


@dataclass(frozen=True, slots=True, order=True)
class TrialKey:
    """Stable identity of one independent experiment repetition."""

    condition_id: str
    case_id: str
    repetition: int


@dataclass(frozen=True, slots=True)
class TrialDimensions:
    """Trusted facts; ``None`` means not evaluated or not applicable."""

    artifact_correct: bool | None = None
    agent_completed: bool | None = None
    scope_compliant: bool | None = None
    cleanup_confirmed: bool | None = None
    behavior_correct: bool | None = None
    fault_triggered: bool | None = None
    recovered: bool | None = None
    safety_triggered: bool | None = None
    safety_blocked: bool | None = None
    legitimate_action_allowed: bool | None = None
    memory_constraint_preserved: bool | None = None
    memory_latest_correction_used: bool | None = None
    memory_stale_state_avoided: bool | None = None
    memory_session_isolated: bool | None = None


@dataclass(frozen=True, slots=True)
class EvalTurnRecord:
    """Only bounded status facts; never model summaries or tool output."""

    turn_index: int
    ok: bool
    verification: str
    rounds: int
    tool_calls: int
    terminal_tool_error: str | None = None
    finish_outcome: str | None = None
    stop_reason: str | None = None
    validation_status: str | None = None
    unresolved_check_count: int | None = None
    verification_required: bool | None = None

    def __post_init__(self) -> None:
        for value, allowed in (
            (self.finish_outcome, ("completed", "incomplete")),
            (self.stop_reason, tuple(reason.value for reason in TaskStopReason)),
            (self.validation_status, ("unverified", "observed", "failed", "stale")),
        ):
            if value is not None and (type(value) is not str or value not in allowed):
                raise ValueError("eval turn diagnostic is invalid")
        if self.unresolved_check_count is not None and (
            type(self.unresolved_check_count) is not int or self.unresolved_check_count < 0
        ):
            raise ValueError("eval turn diagnostic count is invalid")
        if self.verification_required is not None and type(self.verification_required) is not bool:
            raise ValueError("eval turn diagnostic flag is invalid")
        if (type(self.turn_index) is not int or self.turn_index < 1
            or type(self.ok) is not bool
            or type(self.rounds) is not int or self.rounds < 0
            or type(self.tool_calls) is not int or self.tool_calls < 0
            or self.verification not in ("passed", "failed", "not-run", "待验证", "unknown")
            or (self.terminal_tool_error is not None and self.terminal_tool_error not in tuple(code.value for code in ErrorCode))):
            raise ValueError("eval turn record is invalid")


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """Safe per-trial facts used by aggregation and persistence."""

    key: TrialKey
    execution_kind: ExecutionKind
    status: TrialStatus
    dimensions: TrialDimensions
    failure_stage: str | None = None
    failure_codes: tuple[str, ...] = ()
    duration_ms: int | None = None
    rounds: int | None = None
    tool_calls: int | None = None
    retries: int | None = None
    usage: TokenUsage | None = None
    summary_usage: TokenUsage | None = None
    triggered_faults: tuple[str, ...] = ()
    recovery_path: str | None = None
    summary_triggered: bool | None = None
    summary_count: int | None = None
    dangerous_actions_proposed: int | None = None
    dangerous_actions_executed: int | None = None
    safety_bypass_attempts: int | None = None
    legitimate_actions_attempted: int | None = None
    legitimate_actions_allowed: int | None = None
    category: str = "unknown"
    # 与原始 Agent/验证事实分开；blocked 契约可以达成期望但仍保持
    # agent_completed=False 和原始 agent_failed failure code。
    expectation_met: bool | None = None
    assertion_results: tuple["AssertionResult", ...] = ()
    subcase_results: tuple["SubcaseResult", ...] = ()
    turn_results: tuple[EvalTurnRecord, ...] = ()
    first_failed_turn: int | None = None
    fault_evidence: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """A trusted control-plane step loaded from a fixed enum."""

    kind: ScenarioStepKind
    content: str | None = None
    target: str | None = None

    def __post_init__(self) -> None:
        allowed = {
            "user_turn", "memory_save", "memory_refresh", "restart_session",
            "switch_session", "approve", "deny", "undo",
        }
        if self.kind not in allowed:
            raise ValueError("scenario step kind is unsupported")
        if self.kind == "user_turn":
            if not isinstance(self.content, str) or not self.content.strip():
                raise ValueError("user_turn requires non-empty content")
        elif self.content is not None:
            raise ValueError("control steps cannot carry model content")
        if self.kind == "switch_session":
            if not isinstance(self.target, str) or not self.target.strip():
                raise ValueError("switch_session requires a target")
        elif self.target is not None:
            raise ValueError("only switch_session accepts a target")


@dataclass(frozen=True, slots=True)
class PlannedTrial:
    """One frozen row in the experiment schedule."""

    ordinal: int
    key: TrialKey
    case: EvalCase
    condition: EvalCondition


@dataclass(frozen=True, slots=True)
class ExperimentRunReport:
    """Versioned experiment result assembled from incrementally stored trials."""

    run_id: str
    experiment_id: str
    suite_id: str
    started_at: str
    duration_ms: int
    planned_trials: int
    complete: bool
    trials: tuple[TrialRecord, ...]
    benchmark_version: str = "unknown"
    conditions: tuple[EvalCondition, ...] = ()
    fingerprints: tuple[tuple[str, str], ...] = ()
