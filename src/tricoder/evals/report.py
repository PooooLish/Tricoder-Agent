"""Safe JSON and Markdown renderers for deterministic eval reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Final

from tricoder.models import TokenUsage

from .runner import EvalCaseResult, EvalRunReport, VerificationResult


_SCHEMA_VERSION: Final = 1
_FAILURE_CODES: Final = frozenset(
    {
        "agent_failed",
        "agent_unverified",
        "verification_failed",
        "change_out_of_scope",
        "required_change_missing",
        "executor_error",
        "workspace_error",
        "verification_error",
    }
)


def report_as_dict(report: EvalRunReport) -> dict[str, object]:
    """Return only the fixed, non-free-text fields safe to persist."""

    total_cases = len(report.cases)
    passed_cases = sum(case.status == "passed" for case in report.cases)
    return {
        "schema_version": _SCHEMA_VERSION,
        "run_id": report.run_id,
        "suite_id": report.suite_id,
        "provider": report.provider,
        "model": report.model,
        "started_at": report.started_at,
        "duration_ms": report.duration_ms,
        "pass_rate": passed_cases / total_cases if total_cases else 0.0,
        "usage": _usage_as_dict(report.usage),
        "cases": [_case_as_dict(case) for case in report.cases],
    }


def render_markdown(report: EvalRunReport) -> str:
    """Render the case-level safe metrics as a compact Markdown table."""

    lines = [
        "# Eval report",
        "",
        "| Case ID | Status | Duration (ms) | Rounds | Tool calls | Modified files | Verification | Failure codes |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for case in report.cases:
        verifications = "; ".join(
            f"{_markdown_cell(result.name)}: {'passed' if result.passed else 'failed'}"
            for result in case.verifications
        ) or "-"
        failure_codes = ", ".join(_safe_failure_codes(case.failure_codes)) or "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(case.case_id),
                    _safe_status(case.status),
                    str(case.duration_ms),
                    str(case.rounds),
                    str(case.tool_calls),
                    str(len(case.modified_files)),
                    verifications,
                    failure_codes,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_reports(report: EvalRunReport, run_dir: Path) -> tuple[Path, Path]:
    """Atomically write fixed report files within an absolute run directory."""

    if not run_dir.is_absolute():
        raise ValueError("run_dir must be an absolute path")
    directory = run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = _output_path(directory, "result.json")
    markdown_path = _output_path(directory, "report.md")
    _atomic_write(json_path, json.dumps(report_as_dict(report), ensure_ascii=False, indent=2) + "\n")
    _atomic_write(markdown_path, render_markdown(report))
    return json_path, markdown_path


def _case_as_dict(case: EvalCaseResult) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "status": _safe_status(case.status),
        "failure_codes": list(_safe_failure_codes(case.failure_codes)),
        "duration_ms": case.duration_ms,
        "rounds": case.rounds,
        "tool_calls": case.tool_calls,
        "modified_file_count": len(case.modified_files),
        "verifications": [_verification_as_dict(result) for result in case.verifications],
        "agent_verification": "passed" if case.agent_verification == "通过" else "not_passed",
        "usage": _usage_as_dict(case.usage),
    }


def _verification_as_dict(result: VerificationResult) -> dict[str, object]:
    return {
        "name": result.name,
        "exit_code": result.exit_code,
        "passed": result.passed,
        "error_code": result.error_code,
    }


def _usage_as_dict(usage: TokenUsage | None) -> dict[str, int | None]:
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "cache_miss_tokens": None,
        }
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": usage.cached_tokens,
        "cache_miss_tokens": usage.cache_miss_tokens,
    }


def _safe_failure_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(code for code in codes if code in _FAILURE_CODES)


def _safe_status(status: str) -> str:
    return status if status in {"passed", "failed", "error"} else "error"


def _markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _output_path(directory: Path, filename: str) -> Path:
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("report output escapes run directory")
    return path


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
