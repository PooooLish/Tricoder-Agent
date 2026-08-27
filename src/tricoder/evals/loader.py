"""Strict, side-effect-free loading for eval suite definitions."""

from __future__ import annotations

import math
import os
import re
import stat
import tomllib
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any

from tricoder.policy import (
    CommandPolicy,
    PolicyError,
    is_sensitive_workspace_path,
)

from .models import EvalCase, EvalSuite, VerificationSpec


_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_VERIFIER_DIR = ".tricoder_eval_verifier"
_SUITE_FIELDS = frozenset({"id", "title", "cases"})
_CASE_FIELDS = frozenset(
    {
        "id",
        "title",
        "task",
        "allowed_changes",
        "required_changes",
        "max_rounds",
        "max_context_chars",
        "verification",
    }
)
_VERIFICATION_FIELDS = frozenset({"name", "command", "timeout"})

MAX_EVAL_CASES = 32
MAX_CASE_VERIFICATIONS = 8
MAX_TASK_CHARS = 8_000
MAX_VERIFICATION_COMMAND_CHARS = 2_048
MAX_CHANGE_PATTERNS = 64
MAX_CHANGE_PATTERN_CHARS = 256
MAX_CASE_ROUNDS = 64
MAX_CASE_CONTEXT_CHARS = 200_000
MAX_VERIFICATION_TIMEOUT = 300.0
MAX_FIXTURE_FILES = 256
MAX_FIXTURE_BYTES = 1_000_000
MAX_FIXTURE_ENTRIES = 512
MAX_FIXTURE_DEPTH = 32
MAX_DEFINITION_BYTES = 256_000
_RESOURCE_LIMIT_PREFIX = "评测定义超过资源上限"


class EvalDefinitionError(ValueError):
    """评测定义不完整、越界或包含不安全命令。"""


def load_suite(path: Path, *, case_id: str | None = None) -> EvalSuite:
    """Load a fully validated eval suite without constructing a Provider."""

    suite_dir = _require_directory(path, "评测套件目录")
    suite_data = _load_toml(suite_dir / "suite.toml", boundary=suite_dir)
    _require_exact_fields(suite_data, _SUITE_FIELDS, "suite.toml")
    suite_id = _require_id(suite_data["id"], "suite ID")
    title = _require_text(suite_data["title"], "suite title")
    declared_cases = _require_case_ids(suite_data["cases"])

    cases = tuple(_load_case(suite_dir, declared_id) for declared_id in declared_cases)
    if case_id is not None:
        case_id = _require_id(case_id, "case_id")
        if case_id not in declared_cases:
            raise EvalDefinitionError(f"找不到 case：{case_id}")
        cases = tuple(case for case in cases if case.id == case_id)
    return EvalSuite(id=suite_id, title=title, source_dir=suite_dir, cases=cases)


def _load_case(suite_dir: Path, declared_id: str) -> EvalCase:
    cases_dir = _require_directory(suite_dir / "cases", "cases 目录", boundary=suite_dir)
    case_dir = _require_directory(
        cases_dir / declared_id,
        f"case 目录：{declared_id}",
        boundary=cases_dir,
    )
    expected_dir = (suite_dir / "cases" / declared_id).resolve()
    if case_dir != expected_dir:
        raise EvalDefinitionError(f"case 目录不符合预期：{declared_id}")

    case_data = _load_toml(case_dir / "case.toml", boundary=case_dir)
    _require_exact_fields(case_data, _CASE_FIELDS, f"case.toml ({declared_id})")
    parsed_id = _require_id(case_data["id"], "case ID")
    if parsed_id != declared_id:
        raise EvalDefinitionError(f"case ID 与目录不一致：{declared_id}")
    workspace_dir = _require_directory(
        case_dir / "workspace", "workspace 目录", boundary=case_dir
    )
    verifier_dir = _require_directory(
        case_dir / "verifier", "verifier 目录", boundary=case_dir
    )
    if workspace_dir == verifier_dir:
        raise EvalDefinitionError("workspace 与 verifier 目录必须不同")
    fixture_budget = [0, 0, 0]
    _validate_regular_tree(
        workspace_dir,
        fixture_budget,
        reject_reserved_paths=True,
    )
    _validate_regular_tree(
        verifier_dir,
        fixture_budget,
        reject_reserved_paths=False,
    )

    verifications = _load_verifications(case_data["verification"])
    return EvalCase(
        id=parsed_id,
        title=_require_text(case_data["title"], "case title"),
        task=_require_text(
            case_data["task"],
            "case task",
            max_chars=MAX_TASK_CHARS,
        ),
        source_dir=case_dir,
        workspace_dir=workspace_dir,
        verifier_dir=verifier_dir,
        allowed_changes=_require_patterns(case_data["allowed_changes"], "allowed_changes"),
        required_changes=_require_patterns(case_data["required_changes"], "required_changes"),
        max_rounds=_require_positive_int(
            case_data["max_rounds"],
            "max_rounds",
            maximum=MAX_CASE_ROUNDS,
        ),
        max_context_chars=_require_positive_int(
            case_data["max_context_chars"],
            "max_context_chars",
            maximum=MAX_CASE_CONTEXT_CHARS,
        ),
        verifications=verifications,
    )


def _load_verifications(raw: Any) -> tuple[VerificationSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise EvalDefinitionError("verification 必须是非空数组")
    if len(raw) > MAX_CASE_VERIFICATIONS:
        _raise_resource_limit("verification 数量")
    specs: list[VerificationSpec] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise EvalDefinitionError(f"verification #{index} 必须是表")
        _require_exact_fields(item, _VERIFICATION_FIELDS, f"verification #{index}")
        command = _require_text(
            item["command"],
            f"verification #{index} command",
            max_chars=MAX_VERIFICATION_COMMAND_CHARS,
        )
        try:
            CommandPolicy().validate(command)
        except PolicyError as exc:
            raise EvalDefinitionError(f"验证命令不安全：{exc}") from exc
        specs.append(
            VerificationSpec(
                name=_require_text(item["name"], f"verification #{index} name"),
                command=command,
                timeout=_require_positive_number(
                    item["timeout"],
                    f"verification #{index} timeout",
                    maximum=MAX_VERIFICATION_TIMEOUT,
                ),
            )
        )
    return tuple(specs)


def _load_toml(path: Path, *, boundary: Path) -> dict[str, Any]:
    path = _require_regular_file(path, f"定义文件：{path.name}", boundary=boundary)
    try:
        if path.stat().st_size > MAX_DEFINITION_BYTES:
            _raise_resource_limit("定义文件字节数")
    except OSError as exc:
        raise EvalDefinitionError(f"无法读取 {path.name}") from exc
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EvalDefinitionError(f"无法解析 {path.name}") from exc
    if not isinstance(data, dict):
        raise EvalDefinitionError(f"{path.name} 必须是 TOML 表")
    return data


def _require_directory(path: Path, label: str, *, boundary: Path | None = None) -> Path:
    try:
        if _is_link_or_reparse_point(path):
            raise EvalDefinitionError(f"{label} 不能是链接或 junction")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise EvalDefinitionError(f"缺少 {label}") from exc
    if not resolved.is_dir():
        raise EvalDefinitionError(f"{label} 必须是目录")
    if boundary is not None and not resolved.is_relative_to(boundary.resolve(strict=True)):
        raise EvalDefinitionError(f"{label} 超出目录边界")
    return resolved


def _require_regular_file(path: Path, label: str, *, boundary: Path) -> Path:
    try:
        if _is_link_or_reparse_point(path):
            raise EvalDefinitionError(f"{label} 不能是链接或 reparse point")
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise EvalDefinitionError(f"缺少 {label}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise EvalDefinitionError(f"{label} 必须是普通文件")
    if not resolved.is_relative_to(boundary.resolve(strict=True)):
        raise EvalDefinitionError(f"{label} 超出目录边界")
    return resolved


def _validate_regular_tree(
    root: Path,
    budget: list[int],
    *,
    reject_reserved_paths: bool,
) -> None:
    """Iteratively reject unsafe or over-budget fixture entries."""

    root = _require_directory(root, "fixture 目录")
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, parent_depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                remaining_entries = MAX_FIXTURE_ENTRIES - budget[2]
                scanned = tuple(islice(entries, remaining_entries + 1))
        except OSError as exc:
            raise EvalDefinitionError("fixture 路径不可用") from exc
        if len(scanned) > remaining_entries:
            _raise_resource_limit("fixture entry 数")
        for entry in scanned:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise EvalDefinitionError("fixture 路径不可用") from exc
            if entry.is_symlink() or _is_reparse_stat(metadata):
                raise EvalDefinitionError(
                    f"fixture 不能包含链接或 reparse point：{entry.name}"
                )
            path = Path(entry.path)
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise EvalDefinitionError("fixture 路径不可用") from exc
            if not resolved.is_relative_to(root):
                raise EvalDefinitionError("fixture 路径超出目录边界")

            relative = resolved.relative_to(root).as_posix()
            if is_sensitive_workspace_path(relative):
                raise EvalDefinitionError(f"fixture 不能包含敏感路径：{entry.name}")
            if reject_reserved_paths and is_reserved_eval_path(relative):
                raise EvalDefinitionError("workspace fixture 不能包含保留 verifier 目录")

            entry_depth = parent_depth + 1
            budget[2] += 1
            if budget[2] > MAX_FIXTURE_ENTRIES:
                _raise_resource_limit("fixture entry 数")
            if entry_depth > MAX_FIXTURE_DEPTH:
                _raise_resource_limit("fixture 最大深度")

            if stat.S_ISDIR(metadata.st_mode):
                pending.append((path, entry_depth))
            elif stat.S_ISREG(metadata.st_mode):
                budget[0] += 1
                budget[1] += metadata.st_size
                if budget[0] > MAX_FIXTURE_FILES:
                    _raise_resource_limit("fixture 文件数")
                if budget[1] > MAX_FIXTURE_BYTES:
                    _raise_resource_limit("fixture 总字节数")
            else:
                raise EvalDefinitionError(
                    f"fixture 只允许普通文件和目录：{entry.name}"
                )


def _is_link_or_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows reparse points before resolving fixtures."""

    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return _is_reparse_attributes(attributes)


def _is_reparse_stat(metadata: os.stat_result) -> bool:
    return _is_reparse_attributes(getattr(metadata, "st_file_attributes", 0))


def _is_reparse_attributes(attributes: int) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _require_exact_fields(data: dict[str, Any], expected: frozenset[str], label: str) -> None:
    actual = set(data)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise EvalDefinitionError(f"{label} 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        raise EvalDefinitionError(f"{label} 包含未知字段：{', '.join(sorted(unknown))}")


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise EvalDefinitionError(f"{label} 格式无效")
    return value


def _require_text(
    value: Any,
    label: str,
    *,
    max_chars: int | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalDefinitionError(f"{label} 必须是非空文本")
    if max_chars is not None and len(value) > max_chars:
        _raise_resource_limit(f"{label} 长度")
    return value


def _require_case_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvalDefinitionError("cases 必须是非空数组")
    if len(value) > MAX_EVAL_CASES:
        _raise_resource_limit("case 数量")
    ids = tuple(_require_id(item, "case ID") for item in value)
    if len(set(ids)) != len(ids):
        raise EvalDefinitionError("cases 包含重复 case ID")
    return ids


def _require_patterns(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvalDefinitionError(f"{label} 必须是非空数组")
    if len(value) > MAX_CHANGE_PATTERNS:
        _raise_resource_limit(f"{label} 数量")
    return tuple(_require_pattern(item, label) for item in value)


def _require_pattern(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvalDefinitionError(f"{label} 模式必须是非空文本")
    if len(value) > MAX_CHANGE_PATTERN_CHARS:
        _raise_resource_limit(f"{label} 模式长度")
    if "\\" in value:
        raise EvalDefinitionError(f"{label} 模式必须使用 POSIX 路径")
    pattern = PurePosixPath(value)
    if pattern.is_absolute() or ".." in pattern.parts:
        raise EvalDefinitionError(f"{label} 模式必须是相对路径")
    if is_reserved_eval_path(value):
        raise EvalDefinitionError(f"{label} 模式不能触及保留目录")
    return value


def is_reserved_eval_path(path: str) -> bool:
    """Return whether a normalized eval-relative path is framework-owned."""

    return _RESERVED_VERIFIER_DIR in PurePosixPath(path).parts


def _require_positive_int(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvalDefinitionError(f"{label} 必须是正整数")
    if value > maximum:
        _raise_resource_limit(label)
    return value


def _require_positive_number(
    value: Any,
    label: str,
    *,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise EvalDefinitionError(f"{label} 必须是正数")
    try:
        number = float(value)
    except OverflowError as exc:
        raise EvalDefinitionError(f"{label} 必须是正数") from exc
    if not math.isfinite(number):
        raise EvalDefinitionError(f"{label} 必须是正数")
    if number > maximum:
        _raise_resource_limit(label)
    return number


def _raise_resource_limit(label: str) -> None:
    raise EvalDefinitionError(f"{_RESOURCE_LIMIT_PREFIX}：{label}")
