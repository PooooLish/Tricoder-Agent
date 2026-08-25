"""Safe output-root validation and exclusive eval run reservations."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat


_RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")


class EvalOutputError(ValueError):
    """Eval output state is unsafe, occupied, or outside its fixed root."""


def prepare_output_root(project_root: Path) -> Path:
    """Create ``runtime/evals`` only through checked normal directories."""

    if not project_root.is_absolute():
        raise EvalOutputError("project root must be absolute")
    _ensure_normal_directory(project_root, create=False)
    runtime = project_root / "runtime"
    _ensure_normal_directory(runtime, create=True)
    output_root = runtime / "evals"
    _ensure_normal_directory(output_root, create=True)
    return output_root


def reserve_run_directory(
    project_root: Path,
    base_run_id: str,
    *,
    max_attempts: int = 100,
) -> Path:
    """Exclusively reserve a fresh empty run directory with bounded retries."""

    if not _RUN_ID_PATTERN.fullmatch(base_run_id):
        raise EvalOutputError("run id is invalid")
    if type(max_attempts) is not int or max_attempts <= 0:
        raise EvalOutputError("max attempts must be a positive integer")
    output_root = prepare_output_root(project_root)
    for attempt in range(max_attempts):
        run_id = base_run_id if attempt == 0 else f"{base_run_id}-{attempt:02d}"
        run_dir = output_root / run_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        _ensure_normal_directory(run_dir, create=False)
        if any(run_dir.iterdir()):
            raise EvalOutputError("new run directory is not empty")
        return run_dir
    raise EvalOutputError("unable to reserve a unique run directory")


def validate_run_directory(
    project_root: Path,
    run_dir: Path,
    *,
    require_empty: bool,
    create: bool = False,
) -> Path:
    """Validate one direct child of the fixed output root without resolving links."""

    output_root = prepare_output_root(project_root)
    if not run_dir.is_absolute() or run_dir.parent != output_root:
        raise EvalOutputError("run directory must be inside runtime/evals")
    _ensure_normal_directory(run_dir, create=create)
    if require_empty and any(run_dir.iterdir()):
        raise EvalOutputError("run directory must be empty")
    return run_dir


def _ensure_normal_directory(path: Path, *, create: bool) -> None:
    if path.exists() or os.path.lexists(path):
        if _is_link_or_reparse_point(path) or not path.is_dir():
            raise EvalOutputError("output component must be a normal directory")
        return
    if not create:
        raise EvalOutputError("output directory does not exist")
    try:
        path.mkdir()
    except FileExistsError:
        pass
    if _is_link_or_reparse_point(path) or not path.is_dir():
        raise EvalOutputError("output component must be a normal directory")


def _is_link_or_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)
