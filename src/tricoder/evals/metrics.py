"""Pure aggregation for dimensional Agent evaluation metrics."""

from __future__ import annotations

from dataclasses import dataclass

from tricoder.models import TokenUsage

from .models import TrialRecord


_NOT_RUN_STATUSES = frozenset({"not_run_budget", "not_run_cancelled"})
_INFRASTRUCTURE_STAGES = frozenset(
    {"definition", "workspace", "provider", "tool", "cleanup", "report"}
)


@dataclass(frozen=True, slots=True)
class MetricValue:
    numerator: int
    denominator: int
    missing: int
    value: float | None

    @classmethod
    def from_values(
        cls,
        values: tuple[bool | None, ...],
        *,
        denominator: int | None = None,
    ) -> "MetricValue":
        resolved_denominator = len(values) if denominator is None else denominator
        numerator = sum(value is True for value in values)
        missing = sum(value is None for value in values)
        return cls(
            numerator=numerator,
            denominator=resolved_denominator,
            missing=missing,
            value=(
                numerator / resolved_denominator
                if resolved_denominator
                else None
            ),
        )

    def as_tuple(self) -> tuple[int, int, int, float | None]:
        return self.numerator, self.denominator, self.missing, self.value


@dataclass(frozen=True, slots=True)
class TrialMetrics:
    code_correct_planned: MetricValue
    code_correct_verified: MetricValue
    end_to_end: MetricValue
    normal_finish: MetricValue
    infrastructure_errors: int
    unverified: int
    not_run: int
    total_usage: TokenUsage
    total_summary_usage: TokenUsage
    usage_observed: int
    summary_usage_observed: int
    cost_per_success: float | None = None


def aggregate_trial_metrics(trials: tuple[TrialRecord, ...]) -> TrialMetrics:
    """Aggregate trusted facts without coercing missing observations to zero."""

    code_values = tuple(trial.dimensions.artifact_correct for trial in trials)
    verified_values = tuple(value for value in code_values if value is not None)
    finish_values = tuple(trial.dimensions.agent_completed for trial in trials)
    observed_finish = tuple(value for value in finish_values if value is not None)
    end_to_end_values = tuple(_end_to_end(trial) for trial in trials)
    return TrialMetrics(
        code_correct_planned=MetricValue.from_values(code_values),
        code_correct_verified=MetricValue(
            numerator=sum(value is True for value in verified_values),
            denominator=len(verified_values),
            missing=len(code_values) - len(verified_values),
            value=(
                sum(value is True for value in verified_values) / len(verified_values)
                if verified_values
                else None
            ),
        ),
        end_to_end=MetricValue.from_values(end_to_end_values),
        normal_finish=MetricValue(
            numerator=sum(value is True for value in observed_finish),
            denominator=len(observed_finish),
            missing=len(finish_values) - len(observed_finish),
            value=(
                sum(value is True for value in observed_finish) / len(observed_finish)
                if observed_finish
                else None
            ),
        ),
        infrastructure_errors=sum(
            trial.failure_stage in _INFRASTRUCTURE_STAGES for trial in trials
        ),
        unverified=sum(value is None for value in code_values),
        not_run=sum(trial.status in _NOT_RUN_STATUSES for trial in trials),
        total_usage=_strict_usage_total(trials),
        total_summary_usage=_strict_usage_total(trials, summary=True),
        usage_observed=sum(trial.usage is not None for trial in trials),
        summary_usage_observed=sum(trial.summary_usage is not None for trial in trials),
    )


def _end_to_end(trial: TrialRecord) -> bool | None:
    primary = (
        trial.dimensions.artifact_correct
        if trial.dimensions.artifact_correct is not None
        else trial.dimensions.behavior_correct
    )
    values = (
        primary,
        trial.dimensions.agent_completed,
        trial.dimensions.scope_compliant,
        trial.dimensions.cleanup_confirmed,
    )
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _strict_usage_total(
    trials: tuple[TrialRecord, ...], *, summary: bool = False
) -> TokenUsage:
    def total(field: str) -> int | None:
        if not trials:
            return None
        values: list[int] = []
        for trial in trials:
            usage = trial.summary_usage if summary else trial.usage
            if usage is None:
                return None
            value = getattr(usage, field)
            if value is None:
                return None
            values.append(value)
        return sum(values)

    return TokenUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        cached_tokens=total("cached_tokens"),
        cache_miss_tokens=total("cache_miss_tokens"),
    )
