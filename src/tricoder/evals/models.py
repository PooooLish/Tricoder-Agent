"""Immutable domain objects for deterministic eval suites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class EvalSuite:
    id: str
    title: str
    source_dir: Path
    cases: tuple[EvalCase, ...]
