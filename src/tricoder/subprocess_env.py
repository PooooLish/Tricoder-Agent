"""Shared credential filtering for child-process environments."""

from __future__ import annotations

from collections.abc import Mapping
import os
import re
import stat
import sys
from pathlib import Path


_SENSITIVE_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|"
    r"token|password|passwd|secret|credential|authorization)"
)
_PRESERVED_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "COMSPEC",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "LC_ALL",
        "LANG",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "TERM",
        "COLORTERM",
    }
)


def filtered_subprocess_env(
    source: Mapping[str, str] | None = None,
    *,
    excluded_paths: tuple[Path, ...] = (),
) -> dict[str, str]:
    """Copy an environment while removing variables likely to hold credentials."""

    env = dict(os.environ if source is None else source)
    for name in list(env):
        if name in _PRESERVED_ENV:
            continue
        if _SENSITIVE_ENV_RE.search(name):
            del env[name]
    # Verification and Agent test commands run inside scored workspaces.  Force
    # Python to keep those runs observational instead of creating __pycache__
    # files that would be mistaken for Agent-authored changes.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Prevent pytest from writing .pytest_cache inside a scored workspace and
    # neutralize caller-supplied addopts that would bypass command validation.
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    if excluded_paths:
        env["PATH"] = _filtered_search_path(
            env.get("PATH", ""),
            excluded_paths,
        )
    return env


def trusted_python_executable() -> str:
    """Return the current interpreter after resolving it to a regular file."""

    try:
        executable = Path(sys.executable).resolve(strict=True)
        metadata = executable.lstat()
    except OSError as exc:
        raise ValueError("当前 Python 可执行程序不可用") from exc
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
        raise ValueError("当前 Python 可执行程序不可信")
    return str(executable)


def trusted_path_executable(name: str, env: Mapping[str, str]) -> str:
    """Resolve an executable only from explicit, already-filtered PATH entries."""

    for directory_text in env.get("PATH", "").split(os.pathsep):
        if not directory_text:
            continue
        directory = Path(directory_text)
        if not directory.is_absolute():
            continue
        for filename in _candidate_names(name, env):
            candidate = directory / filename
            try:
                metadata = candidate.lstat()
            except OSError:
                continue
            if (
                candidate.is_symlink()
                or _is_reparse_point(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or (os.name != "nt" and not os.access(candidate, os.X_OK))
            ):
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved_metadata = resolved.lstat()
            except OSError:
                continue
            if (
                not stat.S_ISREG(resolved_metadata.st_mode)
                or _is_reparse_point(resolved_metadata)
            ):
                continue
            return str(resolved)
    raise ValueError(f"找不到可信的 {name} 可执行程序")


def _filtered_search_path(path_value: str, excluded_paths: tuple[Path, ...]) -> str:
    excluded = tuple(path.resolve(strict=False) for path in excluded_paths)
    kept: list[str] = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve(strict=False)
        if any(
            resolved == blocked or resolved.is_relative_to(blocked)
            for blocked in excluded
        ):
            continue
        kept.append(str(resolved))
    return os.pathsep.join(kept)


def _candidate_names(name: str, env: Mapping[str, str]) -> tuple[str, ...]:
    if os.name != "nt":
        return (name,)
    # Native executables only: batch files introduce an implicit command shell.
    return (f"{name}.exe",)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)
