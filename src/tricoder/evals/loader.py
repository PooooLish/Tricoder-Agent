"""Strict, side-effect-free loading for eval suite definitions."""

from __future__ import annotations

import math
import re
import stat
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from tricoder.policy import CommandPolicy, PolicyError

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


class EvalDefinitionError(ValueError):
    """评测定义不完整、越界或包含不安全命令。"""


def load_suite(path: Path, *, case_id: str | None = None) -> EvalSuite:
    """Load a fully validated eval suite without constructing a Provider."""

    suite_dir = _require_directory(path, "评测套件目录")
    suite_data = _load_toml(suite_dir / "suite.toml")
    _require_exact_fields(suite_data, _SUITE_FIELDS, "suite.toml")
    suite_id = _require_id(suite_data["id"], "suite ID")
    title = _require_text(suite_data["title"], "suite title")
    declared_cases = _require_case_ids(suite_data["cases"])

    if case_id is not None:
        case_id = _require_id(case_id, "case_id")
        if case_id not in declared_cases:
            raise EvalDefinitionError(f"找不到 case：{case_id}")
        declared_cases = (case_id,)

    cases = tuple(_load_case(suite_dir, declared_id) for declared_id in declared_cases)
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

    case_data = _load_toml(case_dir / "case.toml")
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

    verifications = _load_verifications(case_data["verification"])
    return EvalCase(
        id=parsed_id,
        title=_require_text(case_data["title"], "case title"),
        task=_require_text(case_data["task"], "case task"),
        source_dir=case_dir,
        workspace_dir=workspace_dir,
        verifier_dir=verifier_dir,
        allowed_changes=_require_patterns(case_data["allowed_changes"], "allowed_changes"),
        required_changes=_require_patterns(case_data["required_changes"], "required_changes"),
        max_rounds=_require_positive_int(case_data["max_rounds"], "max_rounds"),
        max_context_chars=_require_positive_int(
            case_data["max_context_chars"], "max_context_chars"
        ),
        verifications=verifications,
    )


def _load_verifications(raw: Any) -> tuple[VerificationSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise EvalDefinitionError("verification 必须是非空数组")
    specs: list[VerificationSpec] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise EvalDefinitionError(f"verification #{index} 必须是表")
        _require_exact_fields(item, _VERIFICATION_FIELDS, f"verification #{index}")
        command = _require_text(item["command"], f"verification #{index} command")
        try:
            CommandPolicy().validate(command)
        except PolicyError as exc:
            raise EvalDefinitionError(f"验证命令不安全：{exc}") from exc
        specs.append(
            VerificationSpec(
                name=_require_text(item["name"], f"verification #{index} name"),
                command=command,
                timeout=_require_positive_number(item["timeout"], f"verification #{index} timeout"),
            )
        )
    return tuple(specs)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvalDefinitionError(f"缺少定义文件：{path.name}")
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
    if boundary is not None and not resolved.is_relative_to(boundary):
        raise EvalDefinitionError(f"{label} 超出目录边界")
    return resolved


def _is_link_or_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows reparse points before resolving fixtures."""

    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
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


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalDefinitionError(f"{label} 必须是非空文本")
    return value


def _require_case_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvalDefinitionError("cases 必须是非空数组")
    ids = tuple(_require_id(item, "case ID") for item in value)
    if len(set(ids)) != len(ids):
        raise EvalDefinitionError("cases 包含重复 case ID")
    return ids


def _require_patterns(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvalDefinitionError(f"{label} 必须是非空数组")
    return tuple(_require_pattern(item, label) for item in value)


def _require_pattern(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvalDefinitionError(f"{label} 模式必须是非空文本")
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


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvalDefinitionError(f"{label} 必须是正整数")
    return value


def _require_positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise EvalDefinitionError(f"{label} 必须是正数")
    try:
        number = float(value)
    except OverflowError as exc:
        raise EvalDefinitionError(f"{label} 必须是正数") from exc
    if not math.isfinite(number):
        raise EvalDefinitionError(f"{label} 必须是正数")
    return number
