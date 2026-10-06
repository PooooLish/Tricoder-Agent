"""任务前工作区内容基线与可确认的完整差异实现。"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from tricoder.changes import DirectorySnapshot, FileSnapshot, TaskChangeSet
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.policy import is_sensitive_workspace_path
from tricoder.workspace.lock import CONTROL_DIRECTORY
from tricoder.workspace.verification import (
    _bound_directory,
    _is_reparse,
    _metadata,
    _open_binary,
)


SNAPSHOT_SCOPE_VERSION = "workspace-baseline-v1"
_CACHE_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", ".pytest_cache"})
_CONTROL_PREFIX = CONTROL_DIRECTORY.as_posix().lower()
_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    max_files: int = 5_000
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_text_bytes: int = 16 * 1024 * 1024
    timeout_seconds: float = 10.0
    max_depth: int = 32

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_text_bytes,
            self.max_depth,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("快照上限必须是正整数")
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("快照超时必须大于零")


class WorkspaceScanError(RuntimeError):
    """只暴露固定失败类别，不泄漏底层路径或异常正文。"""

    _ALLOWED = {
        "permission_denied",
        "limit_exceeded",
        "unstable",
        "unsupported_entry",
        "cancelled",
        "io_error",
    }

    def __init__(self, reason: str) -> None:
        if reason not in self._ALLOWED:
            reason = "io_error"
        self.reason = reason
        super().__init__(reason)


class _ScanAbort(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _InventoryEntry:
    kind: str
    size: int
    mode: int
    identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FileSnapshotEntry:
    path: str
    kind: str
    size: int
    digest: str
    mode: int
    identity: tuple[int, ...] = field(repr=False)
    text: str | None = field(default=None, repr=False, compare=False)

    @property
    def displayable_text(self) -> bool:
        return self.kind == "text" and self.text is not None


@dataclass(frozen=True, slots=True)
class WorkspaceBaseline:
    workspace_key: str
    root_identity: tuple[int, ...] = field(repr=False)
    scope_version: str
    snapshot_id: str
    complete: bool
    entries: tuple[FileSnapshotEntry, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceChange:
    path: str
    change_type: str
    before: FileSnapshotEntry | None = field(default=None, repr=False)
    after: FileSnapshotEntry | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WorkspaceChangePreview:
    workspace_key: str
    baseline_id: str
    candidate_id: str
    changes: tuple[WorkspaceChange, ...]
    pages: tuple[str, ...]
    full_diff: str = field(repr=False)
    complete: bool = True

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)


def _canonical_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=True))))


def _check_progress(
    started: float,
    limits: SnapshotLimits,
    cancellation: CancellationToken | None,
) -> None:
    if cancellation is not None:
        try:
            cancellation.raise_if_cancelled()
        except CancellationError as exc:
            raise _ScanAbort("cancelled") from exc
    if time.monotonic() - started >= limits.timeout_seconds:
        raise _ScanAbort("limit_exceeded")


def _is_excluded(relative: str, *, is_directory: bool) -> bool:
    lowered = relative.lower()
    if lowered == _CONTROL_PREFIX or lowered.startswith(_CONTROL_PREFIX + "/"):
        return True
    name = relative.rsplit("/", 1)[-1].lower()
    if is_directory and name in _CACHE_DIRECTORIES:
        return True
    return is_sensitive_workspace_path(relative)


def _inventory(
    root: Path,
    limits: SnapshotLimits,
    started: float,
    cancellation: CancellationToken | None,
) -> dict[str, _InventoryEntry]:
    entries: dict[str, _InventoryEntry] = {}
    pending = [root]
    total_bytes = 0
    count = 0
    while pending:
        _check_progress(started, limits, cancellation)
        directory = pending.pop()
        directory_metadata = directory.lstat()
        if _is_reparse(directory_metadata) or not stat.S_ISDIR(directory_metadata.st_mode):
            raise _ScanAbort("unsupported_entry")
        with _bound_directory(root, directory) as descriptor:
            bound = os.fstat(descriptor) if descriptor is not None else directory.lstat()
            if _metadata(bound) != _metadata(directory_metadata):
                raise _ScanAbort("unstable")
            with os.scandir(descriptor if descriptor is not None else directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
            for child in children:
                _check_progress(started, limits, cancellation)
                path = directory / child.name
                relative = path.relative_to(root).as_posix()
                metadata = (
                    os.stat(child.name, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor is not None
                    else path.lstat()
                )
                if _is_reparse(metadata):
                    raise _ScanAbort("unsupported_entry")
                is_directory = stat.S_ISDIR(metadata.st_mode)
                if _is_excluded(relative, is_directory=is_directory):
                    continue
                depth = len(Path(relative).parts)
                if depth > limits.max_depth:
                    raise _ScanAbort("limit_exceeded")
                count += 1
                if count > limits.max_files:
                    raise _ScanAbort("limit_exceeded")
                if is_directory:
                    kind = "directory"
                    size = 0
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    kind = "file"
                    size = metadata.st_size
                    if size > limits.max_file_bytes:
                        raise _ScanAbort("limit_exceeded")
                    total_bytes += size
                    if total_bytes > limits.max_total_bytes:
                        raise _ScanAbort("limit_exceeded")
                else:
                    raise _ScanAbort("unsupported_entry")
                entries[relative] = _InventoryEntry(
                    kind=kind,
                    size=size,
                    mode=stat.S_IMODE(metadata.st_mode),
                    identity=_metadata(metadata),
                )
    return entries


def _looks_sensitive_content(text: str) -> bool:
    lowered = text.lower()
    if "-----begin " in lowered and " private key-----" in lowered:
        return True
    for line in lowered.splitlines():
        compact = line.strip().replace(" ", "")
        if "=" not in compact:
            continue
        name, value = compact.split("=", 1)
        if value and any(marker in name for marker in ("api_key", "apikey", "password", "token", "secret")):
            return True
    return False


def _read_entry(
    path: Path,
    expected: _InventoryEntry,
    *,
    root: Path,
    limits: SnapshotLimits,
    started: float,
    cancellation: CancellationToken | None,
) -> tuple[str, str, str | None, int]:
    """读取普通文件并返回 kind、hash、可展示文本和正文缓存字节数。"""

    content_hash = hashlib.sha256()
    blocks: list[bytes] = []
    size = 0
    with _bound_directory(root, path.parent) as descriptor:
        with _open_binary(path, dir_fd=descriptor) as source:
            opened = os.fstat(source.fileno())
            if _is_reparse(opened) or not stat.S_ISREG(opened.st_mode):
                raise _ScanAbort("unsupported_entry")
            if _metadata(opened) != expected.identity:
                raise _ScanAbort("unstable")
            while True:
                _check_progress(started, limits, cancellation)
                block = source.read(_CHUNK)
                if not block:
                    break
                size += len(block)
                if size > limits.max_file_bytes:
                    raise _ScanAbort("limit_exceeded")
                content_hash.update(block)
                blocks.append(block)
            if size != expected.size or _metadata(os.fstat(source.fileno())) != expected.identity:
                raise _ScanAbort("unstable")
    raw = b"".join(blocks)
    try:
        text = None if b"\x00" in raw else raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        return "binary", content_hash.hexdigest(), None, 0
    if _looks_sensitive_content(text):
        return "redacted-text", content_hash.hexdigest(), None, 0
    return "text", content_hash.hexdigest(), text, len(raw)


def capture_workspace_baseline(
    root: Path,
    limits: SnapshotLimits | None = None,
    *,
    cancellation: CancellationToken | None = None,
) -> WorkspaceBaseline:
    """完整扫描工作区；任何遗漏、竞争或上限命中都抛出固定错误。"""

    limits = limits or SnapshotLimits()
    started = time.monotonic()
    try:
        workspace = Path(root).resolve(strict=True)
        root_before = workspace.lstat()
        if _is_reparse(root_before) or not stat.S_ISDIR(root_before.st_mode):
            raise _ScanAbort("unsupported_entry")
        before = _inventory(workspace, limits, started, cancellation)
        entries: list[FileSnapshotEntry] = []
        cached_text_bytes = 0
        for relative, expected in sorted(before.items()):
            _check_progress(started, limits, cancellation)
            if expected.kind == "directory":
                entries.append(FileSnapshotEntry(
                    relative,
                    "directory",
                    0,
                    "",
                    expected.mode,
                    expected.identity,
                ))
                continue
            kind, digest, text, text_bytes = _read_entry(
                workspace / relative,
                expected,
                root=workspace,
                limits=limits,
                started=started,
                cancellation=cancellation,
            )
            cached_text_bytes += text_bytes
            if cached_text_bytes > limits.max_text_bytes:
                raise _ScanAbort("limit_exceeded")
            entries.append(FileSnapshotEntry(
                relative,
                kind,
                expected.size,
                digest,
                expected.mode,
                expected.identity,
                text,
            ))
        after = _inventory(workspace, limits, started, cancellation)
        if before != after or _metadata(workspace.lstat()) != _metadata(root_before):
            raise _ScanAbort("unstable")
        _check_progress(started, limits, cancellation)
    except _ScanAbort as exc:
        raise WorkspaceScanError(exc.reason) from None
    except PermissionError:
        raise WorkspaceScanError("permission_denied") from None
    except CancellationError:
        raise WorkspaceScanError("cancelled") from None
    except (OSError, ValueError, OverflowError, RecursionError):
        raise WorkspaceScanError("io_error") from None

    workspace_key = _canonical_key(workspace)
    encoded = json.dumps(
        [
            SNAPSHOT_SCOPE_VERSION,
            workspace_key,
            list(_metadata(root_before)),
            [
                [entry.path, entry.kind, entry.size, entry.digest, entry.mode, entry.identity]
                for entry in entries
            ],
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return WorkspaceBaseline(
        workspace_key=workspace_key,
        root_identity=_metadata(root_before),
        scope_version=SNAPSHOT_SCOPE_VERSION,
        snapshot_id=hashlib.sha256(encoded).hexdigest(),
        complete=True,
        entries=tuple(entries),
    )


def _escaped_codepoint(character: str) -> str:
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    if character in named:
        return named[character]
    codepoint = ord(character)
    if codepoint <= 0xFF:
        return f"\\x{codepoint:02x}"
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def _escape_untrusted(value: str, *, path: bool = False) -> str:
    """转义终端控制；文件名不能保留任何布局或双向控制字符。"""

    output: list[str] = []
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if not path and character == "\n":
            output.append(character)
        elif codepoint < 32 or codepoint == 127 or category in {"Cc", "Cf"}:
            output.append(_escaped_codepoint(character))
        else:
            output.append(character)
    return "".join(output)


def _entry_changed(before: FileSnapshotEntry, after: FileSnapshotEntry) -> bool:
    return (
        before.kind,
        before.size,
        before.digest,
        before.mode,
        before.identity,
    ) != (
        after.kind,
        after.size,
        after.digest,
        after.mode,
        after.identity,
    )


def _render_change(change: WorkspaceChange) -> str:
    path = _escape_untrusted(change.path, path=True)
    before, after = change.before, change.after
    if before is not None and after is not None and before.displayable_text and after.displayable_text:
        old = _escape_untrusted(before.text or "").splitlines(keepends=True)
        new = _escape_untrusted(after.text or "").splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        ))
    if before is None and after is not None and after.displayable_text:
        new = _escape_untrusted(after.text or "").splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            [], new, fromfile="/dev/null", tofile=f"b/{path}", lineterm="\n"
        ))
    if after is None and before is not None and before.displayable_text:
        old = _escape_untrusted(before.text or "").splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            old, [], fromfile=f"a/{path}", tofile="/dev/null", lineterm="\n"
        ))
    old_kind = "不存在" if before is None else before.kind
    new_kind = "不存在" if after is None else after.kind
    old_size = 0 if before is None else before.size
    new_size = 0 if after is None else after.size
    label = "二进制或不可安全展示内容" if "binary" in {old_kind, new_kind} else "元数据/类型变化"
    return (
        f"--- a/{path}\n+++ b/{path}\n"
        f"[{label}] {old_kind}({old_size} bytes) -> {new_kind}({new_size} bytes)\n"
    )


def compare_baselines(
    before: WorkspaceBaseline,
    after: WorkspaceBaseline,
    *,
    page_chars: int = 32_000,
) -> WorkspaceChangePreview:
    """比较同工作区同范围的两个完整基线，并返回不截断的分页预览。"""

    if type(before) is not WorkspaceBaseline or type(after) is not WorkspaceBaseline:
        raise TypeError("基线类型无效")
    if not before.complete or not after.complete:
        raise ValueError("不完整基线不可比较")
    if before.workspace_key != after.workspace_key or before.scope_version != after.scope_version:
        raise ValueError("工作区或快照范围不兼容")
    if before.root_identity != after.root_identity:
        raise ValueError("工作区根目录身份不兼容")
    if type(page_chars) is not int or page_chars <= 0:
        raise ValueError("分页字符数必须是正整数")
    old: Mapping[str, FileSnapshotEntry] = {entry.path: entry for entry in before.entries}
    new: Mapping[str, FileSnapshotEntry] = {entry.path: entry for entry in after.entries}
    changes: list[WorkspaceChange] = []
    for path in sorted(set(old) | set(new)):
        previous, current = old.get(path), new.get(path)
        if previous is None:
            changes.append(WorkspaceChange(path, "added", None, current))
        elif current is None:
            changes.append(WorkspaceChange(path, "deleted", previous, None))
        elif _entry_changed(previous, current):
            change_type = "type_changed" if previous.kind != current.kind else "modified"
            changes.append(WorkspaceChange(path, change_type, previous, current))
    full_diff = "".join(_render_change(change) for change in changes)
    pages = tuple(
        full_diff[index:index + page_chars]
        for index in range(0, len(full_diff), page_chars)
    )
    return WorkspaceChangePreview(
        workspace_key=before.workspace_key,
        baseline_id=before.snapshot_id,
        candidate_id=after.snapshot_id,
        changes=tuple(changes),
        pages=pages,
        full_diff=full_diff,
    )


def _entry_matches_journal_snapshot(
    entry: FileSnapshotEntry | None,
    expected: FileSnapshot | None,
    *,
    exact_bytes: bool,
    identity_required: bool,
) -> bool:
    """核对工具账本版本，而不只核对相同的相对路径。"""

    if expected is None:
        return entry is None
    if entry is None or entry.kind not in {"text", "redacted-text"}:
        return False
    raw = expected.content.encode("utf-8")
    identity = entry.identity
    content_matches = (
        entry.size == len(raw) and entry.digest == hashlib.sha256(raw).hexdigest()
    )
    if not exact_bytes and entry.kind == "text" and entry.text is not None:
        # 工具的审批前读取使用文本模式，Windows 会把 CRLF 规范为 LF；
        # before 只能以同一对象身份 + 规范文本证明。after 由工具以 newline=""
        # 发布，必须继续做精确字节摘要校验，防止同路径二次覆盖。
        content_matches = content_matches or (
            entry.text.replace("\r\n", "\n").replace("\r", "\n")
            == expected.content
        )
    return (
        content_matches
        and entry.mode == expected.mode
        and (
            not identity_required
            or (
                len(identity) >= 3
                and identity[1] == expected.identity.device
                and identity[2] == expected.identity.inode
            )
        )
    )


def _entry_matches_directory_snapshot(
    entry: FileSnapshotEntry | None,
    expected: DirectorySnapshot | None,
    *,
    identity_required: bool,
) -> bool:
    """按目录种类、权限和本地对象身份核对目录账本。"""

    if expected is None:
        return entry is None
    if entry is None or entry.kind != "directory":
        return False
    return (
        entry.mode == expected.mode
        and (
            not identity_required
            or (
                len(entry.identity) >= 3
                and entry.identity[1] == expected.identity.device
                and entry.identity[2] == expected.identity.inode
            )
        )
    )


def task_changes_match_baselines(
    before: WorkspaceBaseline,
    after: WorkspaceBaseline,
    change_set: TaskChangeSet | None,
    *,
    framework_owned_paths: tuple[str, ...] = (),
    after_identity_required: bool = True,
) -> bool:
    """证明任务末状态只由账本的精确 before/after 与框架文件构成。"""

    if change_set is not None and (
        change_set.tainted_paths or change_set.tainted_directory_paths
    ):
        return False
    comparison = compare_baselines(before, after)
    old = {entry.path: entry for entry in before.entries}
    new = {entry.path: entry for entry in after.entries}
    expected = (
        {change.path: change for change in change_set.changes}
        if change_set is not None
        else {}
    )
    expected_directories = (
        {change.path: change for change in change_set.directory_changes}
        if change_set is not None
        else {}
    )
    owned = set(framework_owned_paths)

    for path, change in expected.items():
        if not _entry_matches_journal_snapshot(
            old.get(path),
            change.before,
            exact_bytes=False,
            identity_required=True,
        ):
            return False
        if not _entry_matches_journal_snapshot(
            new.get(path),
            change.after,
            exact_bytes=True,
            identity_required=after_identity_required,
        ):
            return False

    for path, change in expected_directories.items():
        if not _entry_matches_directory_snapshot(
            old.get(path),
            change.before,
            identity_required=True,
        ):
            return False
        if not _entry_matches_directory_snapshot(
            new.get(path),
            change.after,
            identity_required=after_identity_required,
        ):
            return False

    expected_paths = set(expected)
    expected_directory_paths = set(expected_directories)
    for change in comparison.changes:
        if (
            change.path in owned
            or change.path in expected_paths
            or change.path in expected_directory_paths
        ):
            continue
        is_directory = (
            (change.before is not None and change.before.kind == "directory")
            or (change.after is not None and change.after.kind == "directory")
        )
        if is_directory and any(
            path.startswith(change.path.rstrip("/") + "/")
            for path in expected_paths | owned
        ):
            continue
        return False
    return True
