"""纯路径覆盖规则；调用者负责证据来源、任务、时效与目录身份核验。"""
from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath


def normalize_target(cwd: str, target: str) -> str | None:
    for raw in (cwd, target):
        if not isinstance(raw, str) or PurePosixPath(raw.replace("\\", "/")).is_absolute() or PureWindowsPath(raw).drive:
            return None
    parts = (PurePosixPath(cwd.replace("\\", "/")) / target.replace("\\", "/")).parts
    normalized = []
    for part in parts:
        if part == "..":
            if not normalized:
                return None
            normalized.pop()
        else:
            normalized.append(part)
    return os.path.normcase("/".join(normalized)).replace("\\", "/") or "."


def target_covers_path(target: str, path: str) -> bool:
    target = normalize_target(".", target)
    path = normalize_target(".", path)
    return bool(target is not None and path is not None and (
        target == "." or target == path or path.startswith(target.rstrip("/") + "/")
    ))


def uncovered_paths(paths, targets) -> tuple[str, ...]:
    targets = tuple(targets)
    return tuple(path for path in dict.fromkeys(paths)
                 if not any(target_covers_path(target, path) for target in targets))


def source_verification_paths(files, directories) -> tuple[str, ...]:
    """本轮已确认的结构父目录不额外要求代码检查，独立目录仍要求检查。"""
    files = tuple(files)
    return tuple(dict.fromkeys((*files, *(directory for directory in directories
        if not any(target_covers_path(directory, path) for path in files)))))


def covered_structural_directories(files, directories, targets) -> tuple[str, ...]:
    """仅解除所有本轮受影响子文件已覆盖的结构目录；不判断历史来源。"""
    files, targets = tuple(files), tuple(targets)
    covered = []
    for directory in directories:
        children = tuple(path for path in files if target_covers_path(directory, path))
        if children and not uncovered_paths(children, targets):
            covered.append(directory)
    return tuple(covered)
