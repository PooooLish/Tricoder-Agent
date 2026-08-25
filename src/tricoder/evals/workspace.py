"""Isolated, link-safe workspaces for deterministic local evaluations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
from typing import Mapping

from .loader import is_reserved_eval_path
from .models import EvalCase


RESERVED_VERIFIER_DIR = ".tricoder_eval_verifier"


class WorkspaceSafetyError(ValueError):
    """A workspace operation would follow a link or leave its boundary."""


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    size: int
    sha256: str


def prepare_workspace(case: EvalCase, workspaces_root: Path) -> Path:
    """Copy one case fixture into a new isolated workspace."""

    if Path(case.id).name != case.id:
        raise WorkspaceSafetyError("case ID 不能包含路径分隔符")
    fixture = _ensure_directory(case.workspace_dir)
    verifier_source = _ensure_directory(case.verifier_dir)
    root = workspaces_root.resolve(strict=False)
    workspace = _within_root(root / case.id, root)
    for source in (fixture, verifier_source):
        if _paths_overlap(root, source) or _paths_overlap(workspace, source):
            raise WorkspaceSafetyError("工作副本与评测源目录重叠，无法隔离")
    _validate_fixture_tree(fixture, fixture)

    root = _ensure_directory(workspaces_root, create=True)
    workspace = _within_root(root / case.id, root)
    if os.path.lexists(workspace):
        raise WorkspaceSafetyError(f"工作副本已存在：{case.id}")
    workspace.mkdir()
    _copy_tree(
        fixture,
        workspace,
        workspace,
        reject_reserved_paths=True,
        fixture_root=fixture,
    )
    return workspace


def capture_snapshot(workspace: Path) -> dict[str, FileFingerprint]:
    """Fingerprint ordinary workspace files, excluding framework-owned paths."""

    root = _ensure_directory(workspace)
    snapshot: dict[str, FileFingerprint] = {}
    for path in _iter_regular_files(root, root):
        relative_path = path.relative_to(root).as_posix()
        if is_reserved_eval_path(relative_path):
            continue
        snapshot[relative_path] = _fingerprint(path)
    return snapshot


def changed_paths(
    before: Mapping[str, FileFingerprint], after: Mapping[str, FileFingerprint]
) -> tuple[str, ...]:
    """Return added, removed, and modified paths in deterministic order."""

    return tuple(
        path
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    )


def install_verifier(case: EvalCase, workspace: Path) -> Path:
    """Safely inject the case's hidden verifier after the initial snapshot."""

    root = _ensure_directory(workspace)
    verifier = _verifier_path(root)
    if os.path.lexists(verifier):
        raise WorkspaceSafetyError("保留 verifier 目录已存在")
    verifier.mkdir()
    _copy_tree(case.verifier_dir, verifier, root)
    return verifier


def remove_verifier(workspace: Path) -> None:
    """Remove only the framework-owned verifier directory from a workspace."""

    root = _ensure_directory(workspace)
    verifier = _verifier_path(root)
    if not os.path.lexists(verifier):
        return
    _reject_link_or_reparse_path(verifier)
    if not verifier.is_dir():
        raise WorkspaceSafetyError("保留 verifier 路径必须是目录")
    _remove_tree(verifier, root)


def _ensure_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    _reject_link_or_reparse_path(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceSafetyError(f"目录不可用：{path}") from exc
    if not resolved.is_dir():
        raise WorkspaceSafetyError(f"路径必须是目录：{path}")
    return resolved


def _verifier_path(workspace: Path) -> Path:
    verifier = _within_root(workspace / RESERVED_VERIFIER_DIR, workspace)
    if verifier.name != RESERVED_VERIFIER_DIR or verifier.parent != workspace:
        raise WorkspaceSafetyError("保留 verifier 目录路径无效")
    return verifier


def _within_root(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise WorkspaceSafetyError("路径超出工作副本边界")
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _validate_fixture_tree(directory: Path, fixture_root: Path) -> None:
    directory = _ensure_directory(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            _reject_link_or_reparse_entry(entry)
            path = Path(entry.path)
            if is_reserved_eval_path(path.relative_to(fixture_root).as_posix()):
                raise WorkspaceSafetyError("fixture 不能包含保留 verifier 目录")
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                _validate_fixture_tree(path, fixture_root)
            elif not stat.S_ISREG(entry_stat.st_mode):
                raise WorkspaceSafetyError(f"仅允许复制普通文件和目录：{path.name}")


def _copy_tree(
    source: Path,
    destination: Path,
    root: Path,
    *,
    reject_reserved_paths: bool = False,
    fixture_root: Path | None = None,
) -> None:
    source = _ensure_directory(source)
    fixture_root = fixture_root or source
    destination = _within_root(destination, root)
    with os.scandir(source) as entries:
        for entry in entries:
            _reject_link_or_reparse_entry(entry)
            source_path = Path(entry.path)
            if reject_reserved_paths and is_reserved_eval_path(
                source_path.relative_to(fixture_root).as_posix()
            ):
                raise WorkspaceSafetyError("fixture 不能包含保留 verifier 目录")
            destination_path = _within_root(destination / entry.name, root)
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                destination_path.mkdir()
                _copy_tree(
                    source_path,
                    destination_path,
                    root,
                    reject_reserved_paths=reject_reserved_paths,
                    fixture_root=fixture_root,
                )
            elif stat.S_ISREG(entry_stat.st_mode):
                shutil.copyfile(source_path, destination_path)
            else:
                raise WorkspaceSafetyError(f"仅允许复制普通文件和目录：{source_path.name}")


def _iter_regular_files(directory: Path, root: Path):
    directory = _ensure_directory(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            _reject_link_or_reparse_entry(entry)
            path = _within_root(Path(entry.path), root)
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                yield from _iter_regular_files(path, root)
            elif stat.S_ISREG(entry_stat.st_mode):
                yield path
            else:
                raise WorkspaceSafetyError(f"仅允许快照普通文件和目录：{path.name}")


def _fingerprint(path: Path) -> FileFingerprint:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return FileFingerprint(size=path.stat().st_size, sha256=digest.hexdigest())


def _remove_tree(directory: Path, root: Path) -> None:
    directory = _within_root(directory, root)
    _reject_link_or_reparse_path(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            _reject_link_or_reparse_entry(entry)
            path = _within_root(Path(entry.path), root)
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                _remove_tree(path, root)
            elif stat.S_ISREG(entry_stat.st_mode):
                path.unlink()
            else:
                raise WorkspaceSafetyError(f"仅允许删除普通文件和目录：{path.name}")
    directory.rmdir()


def _reject_link_or_reparse_entry(entry: os.DirEntry[str]) -> None:
    if entry.is_symlink() or _is_reparse_point(entry.stat(follow_symlinks=False)):
        raise WorkspaceSafetyError(f"不允许链接或 reparse point：{entry.name}")


def _reject_link_or_reparse_path(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise WorkspaceSafetyError(f"路径不可用：{path}") from exc
    if path.is_symlink() or _is_reparse_point(path_stat):
        raise WorkspaceSafetyError(f"不允许链接或 reparse point：{path.name}")


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)
