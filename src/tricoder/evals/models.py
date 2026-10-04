"""Immutable domain objects for deterministic eval suites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from tricoder.models import TokenUsage


ExecutionKind: TypeAlias = Literal["quality", "contract"]
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
