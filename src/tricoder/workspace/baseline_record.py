"""不含源码正文的工作区稳定基线记录与文件级比较。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass

from tricoder.workspace.snapshot import WorkspaceBaseline


BASELINE_RECORD_FORMAT_VERSION = 1
MAX_BASELINE_RECORD_ENTRIES = 5_000
MAX_BASELINE_RECORD_PATH_CHARS = 4_096
MAX_BASELINE_RECORD_PAYLOAD_BYTES = 8 * 1024 * 1024
_FILE_KINDS = frozenset({"text", "binary", "redacted-text"})
_HEX = frozenset("0123456789abcdef")


class BaselineRecordError(ValueError):
    """以固定类别报告记录错误，避免把损坏 payload 原文带到界面。"""

    _ALLOWED = {
        "corrupted",
        "incomplete",
        "limit_exceeded",
        "unsupported_format",
    }

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in self._ALLOWED else "corrupted"
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    """单个稳定条目；普通文件不区分文本/二进制展示类型。"""

    path: str
    kind: str
    size: int
    digest: str
    mode: int


@dataclass(frozen=True, slots=True)
class BaselineRecord:
    """可安全持久化的完整工作区投影。"""

    format_version: int
    scope_version: str
    workspace_key: str
    root_identity: tuple[int, int, int]
    complete: bool
    entries: tuple[BaselineEntry, ...]
    content_digest: str


@dataclass(frozen=True, slots=True)
class BaselineRecordChange:
    path: str
    change_type: str


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    compatible: bool
    reason: str | None
    changes: tuple[BaselineRecordChange, ...]

    @property
    def changed(self) -> bool:
        return not self.compatible or bool(self.changes)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)


def _is_safe_text(value: object, *, max_chars: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_chars
        and all(
            character.isprintable()
            and unicodedata.category(character) not in {"Cc", "Cf"}
            for character in value
        )
    )


def _validate_relative_path(value: object) -> str:
    if not _is_safe_text(value, max_chars=MAX_BASELINE_RECORD_PATH_CHARS):
        raise BaselineRecordError("corrupted")
    path = value
    assert isinstance(path, str)
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
    ):
        raise BaselineRecordError("corrupted")
    return path


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _entry_payload(entry: BaselineEntry) -> dict[str, object]:
    return {
        "path": entry.path,
        "kind": entry.kind,
        "size": entry.size,
        "digest": entry.digest,
        "mode": entry.mode,
    }


def _digest_payload(
    *,
    format_version: int,
    scope_version: str,
    workspace_key: str,
    root_identity: tuple[int, int, int],
    complete: bool,
    entries: tuple[BaselineEntry, ...],
) -> str:
    encoded = json.dumps(
        {
            "format_version": format_version,
            "scope_version": scope_version,
            "workspace_key": workspace_key,
            "root_identity": list(root_identity),
            "complete": complete,
            "entries": [_entry_payload(entry) for entry in entries],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_record(record: BaselineRecord) -> BaselineRecord:
    if type(record) is not BaselineRecord:
        raise BaselineRecordError("corrupted")
    if type(record.format_version) is not int:
        raise BaselineRecordError("corrupted")
    if record.format_version != BASELINE_RECORD_FORMAT_VERSION:
        raise BaselineRecordError("unsupported_format")
    if type(record.complete) is not bool:
        raise BaselineRecordError("corrupted")
    if not record.complete:
        raise BaselineRecordError("incomplete")
    if not _is_safe_text(record.scope_version, max_chars=200):
        raise BaselineRecordError("corrupted")
    if not _is_safe_text(record.workspace_key, max_chars=4_096):
        raise BaselineRecordError("corrupted")
    if (
        type(record.root_identity) is not tuple
        or len(record.root_identity) != 3
        or any(type(value) is not int or value < 0 for value in record.root_identity)
    ):
        raise BaselineRecordError("corrupted")
    if type(record.entries) is not tuple:
        raise BaselineRecordError("corrupted")
    if len(record.entries) > MAX_BASELINE_RECORD_ENTRIES:
        raise BaselineRecordError("limit_exceeded")
    previous = ""
    for entry in record.entries:
        if type(entry) is not BaselineEntry:
            raise BaselineRecordError("corrupted")
        path = _validate_relative_path(entry.path)
        if path <= previous:
            raise BaselineRecordError("corrupted")
        previous = path
        if type(entry.kind) is not str or entry.kind not in {"file", "directory"}:
            raise BaselineRecordError("corrupted")
        if type(entry.size) is not int or entry.size < 0:
            raise BaselineRecordError("corrupted")
        if type(entry.digest) is not str:
            raise BaselineRecordError("corrupted")
        if type(entry.mode) is not int or not 0 <= entry.mode <= 0o7777:
            raise BaselineRecordError("corrupted")
        if entry.kind == "directory":
            if entry.size != 0 or entry.digest != "":
                raise BaselineRecordError("corrupted")
        elif not _is_sha256(entry.digest):
            raise BaselineRecordError("corrupted")
    expected = _digest_payload(
        format_version=record.format_version,
        scope_version=record.scope_version,
        workspace_key=record.workspace_key,
        root_identity=record.root_identity,
        complete=record.complete,
        entries=record.entries,
    )
    if not _is_sha256(record.content_digest) or record.content_digest != expected:
        raise BaselineRecordError("corrupted")
    return record


def make_baseline_record(baseline: WorkspaceBaseline) -> BaselineRecord:
    """从新鲜完整扫描生成稳定投影，明确丢弃正文和文件对象 identity。"""

    if type(baseline) is not WorkspaceBaseline:
        raise BaselineRecordError("corrupted")
    if not baseline.complete:
        raise BaselineRecordError("incomplete")
    entries: list[BaselineEntry] = []
    seen: set[str] = set()
    for source in sorted(baseline.entries, key=lambda item: item.path):
        path = _validate_relative_path(source.path)
        if path in seen:
            raise BaselineRecordError("corrupted")
        seen.add(path)
        if source.kind == "directory":
            entry = BaselineEntry(path, "directory", 0, "", source.mode)
        elif source.kind in _FILE_KINDS:
            entry = BaselineEntry(path, "file", source.size, source.digest, source.mode)
        else:
            raise BaselineRecordError("corrupted")
        entries.append(entry)
    if len(entries) > MAX_BASELINE_RECORD_ENTRIES:
        raise BaselineRecordError("limit_exceeded")
    if len(baseline.root_identity) != 3:
        raise BaselineRecordError("corrupted")
    root_identity = tuple(baseline.root_identity)
    if any(type(value) is not int or value < 0 for value in root_identity):
        raise BaselineRecordError("corrupted")
    typed_root = (root_identity[0], root_identity[1], root_identity[2])
    digest = _digest_payload(
        format_version=BASELINE_RECORD_FORMAT_VERSION,
        scope_version=baseline.scope_version,
        workspace_key=baseline.workspace_key,
        root_identity=typed_root,
        complete=True,
        entries=tuple(entries),
    )
    return _validate_record(BaselineRecord(
        format_version=BASELINE_RECORD_FORMAT_VERSION,
        scope_version=baseline.scope_version,
        workspace_key=baseline.workspace_key,
        root_identity=typed_root,
        complete=True,
        entries=tuple(entries),
        content_digest=digest,
    ))


def baseline_record_to_json(record: BaselineRecord) -> str:
    """以规范 JSON 序列化；调用方可安全写入 SQLite。"""

    record = _validate_record(record)
    payload = json.dumps(
        {
            "format_version": record.format_version,
            "scope_version": record.scope_version,
            "workspace_key": record.workspace_key,
            "root_identity": list(record.root_identity),
            "complete": record.complete,
            "entries": [_entry_payload(entry) for entry in record.entries],
            "content_digest": record.content_digest,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > MAX_BASELINE_RECORD_PAYLOAD_BYTES:
        raise BaselineRecordError("limit_exceeded")
    return payload


def baseline_record_from_json(payload: str) -> BaselineRecord:
    """严格解析持久化 payload；损坏与未知格式绝不降级为无记录。"""

    if not isinstance(payload, str):
        raise BaselineRecordError("corrupted")
    try:
        payload_size = len(payload.encode("utf-8"))
    except UnicodeError as exc:
        raise BaselineRecordError("corrupted") from exc
    if payload_size > MAX_BASELINE_RECORD_PAYLOAD_BYTES:
        raise BaselineRecordError("limit_exceeded")
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise BaselineRecordError("corrupted") from exc
    if not isinstance(value, dict):
        raise BaselineRecordError("corrupted")
    version = value.get("format_version")
    if type(version) is not int:
        raise BaselineRecordError("corrupted")
    if version != BASELINE_RECORD_FORMAT_VERSION:
        raise BaselineRecordError("unsupported_format")
    if set(value) != {
        "format_version",
        "scope_version",
        "workspace_key",
        "root_identity",
        "complete",
        "entries",
        "content_digest",
    }:
        raise BaselineRecordError("corrupted")
    if not isinstance(value["scope_version"], str):
        raise BaselineRecordError("corrupted")
    if not isinstance(value["workspace_key"], str):
        raise BaselineRecordError("corrupted")
    if type(value["complete"]) is not bool:
        raise BaselineRecordError("corrupted")
    if not isinstance(value["content_digest"], str):
        raise BaselineRecordError("corrupted")
    root = value["root_identity"]
    if not isinstance(root, list) or len(root) != 3:
        raise BaselineRecordError("corrupted")
    if any(type(item) is not int for item in root):
        raise BaselineRecordError("corrupted")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list):
        raise BaselineRecordError("corrupted")
    if len(raw_entries) > MAX_BASELINE_RECORD_ENTRIES:
        raise BaselineRecordError("limit_exceeded")
    entries: list[BaselineEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != {
            "path", "kind", "size", "digest", "mode"
        }:
            raise BaselineRecordError("corrupted")
        if not isinstance(item["path"], str):
            raise BaselineRecordError("corrupted")
        if not isinstance(item["kind"], str):
            raise BaselineRecordError("corrupted")
        if type(item["size"]) is not int:
            raise BaselineRecordError("corrupted")
        if not isinstance(item["digest"], str):
            raise BaselineRecordError("corrupted")
        if type(item["mode"]) is not int:
            raise BaselineRecordError("corrupted")
        entries.append(BaselineEntry(
            path=item["path"],
            kind=item["kind"],
            size=item["size"],
            digest=item["digest"],
            mode=item["mode"],
        ))
    return _validate_record(BaselineRecord(
        format_version=version,
        scope_version=value["scope_version"],
        workspace_key=value["workspace_key"],
        root_identity=(root[0], root[1], root[2]),
        complete=value["complete"],
        entries=tuple(entries),
        content_digest=value["content_digest"],
    ))


def compare_baseline_records(
    before: BaselineRecord,
    after: BaselineRecord,
) -> BaselineComparison:
    """比较两个稳定记录；不兼容原因与普通文件变化保持可区分。"""

    before = _validate_record(before)
    after = _validate_record(after)
    if before.workspace_key != after.workspace_key:
        return BaselineComparison(False, "workspace_mismatch", ())
    if before.scope_version != after.scope_version:
        return BaselineComparison(False, "scope_version_changed", ())
    if before.root_identity != after.root_identity:
        return BaselineComparison(False, "root_identity_changed", ())
    if before.content_digest == after.content_digest:
        return BaselineComparison(True, None, ())
    old = {entry.path: entry for entry in before.entries}
    new = {entry.path: entry for entry in after.entries}
    changes: list[BaselineRecordChange] = []
    for path in sorted(set(old) | set(new)):
        previous, current = old.get(path), new.get(path)
        if previous is None:
            change_type = "added"
        elif current is None:
            change_type = "deleted"
        elif previous.kind != current.kind:
            change_type = "type_changed"
        elif (
            previous.mode != current.mode
            and previous.size == current.size
            and previous.digest == current.digest
        ):
            change_type = "permission_changed"
        elif previous != current:
            change_type = "modified"
        else:
            continue
        changes.append(BaselineRecordChange(path, change_type))
    return BaselineComparison(True, None, tuple(changes))
