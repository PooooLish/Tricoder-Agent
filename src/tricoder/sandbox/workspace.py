"""Session 隔离的 Docker 执行工作副本。

复制器先按路径判定排除项，再打开普通文件；因此不会为了判断内容而读取 `.env*`、
凭据目录或版本库元数据。任何非普通文件、链接、reparse point、硬链接、预算超限或
复制竞争都会使整个候选失败，半成品不会获得控制清单。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
from types import MappingProxyType
from typing import Literal

from tricoder.core.cancellation import CancellationToken
from tricoder.policy import is_sensitive_workspace_path


_CONTROL_VERSION = 1
_CONTROL_MAX_BYTES = 8 * 1024 * 1024
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SOURCE_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".tricoder",
        ".tricoder_eval_verifier",
        "runtime",
    }
)
_SAFE_RUNTIME_OUTPUT_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_SAFE_RUNTIME_OUTPUT_FILES = frozenset({".coverage"})


class SandboxWorkspaceError(RuntimeError):
    """副本无法在不削弱边界的前提下准备或读取。"""


@dataclass(frozen=True, slots=True)
class CopyBudgets:
    """复制和后续扫描共用的资源上限。"""

    max_entries: int = 10_000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_entries, self.max_file_bytes, self.max_total_bytes)
        ):
            raise ValueError("副本预算必须是正整数")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("单文件预算不能大于总预算")


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    """不含源码正文的版本清单条目。"""

    kind: Literal["file", "directory"]
    mode: int
    size: int
    mtime_ns: int
    device: int
    inode: int
    digest: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
            "digest": self.digest,
        }

    @classmethod
    def from_json(cls, raw: object) -> "WorkspaceEntry":
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "mode",
            "size",
            "mtime_ns",
            "device",
            "inode",
            "digest",
        }:
            raise SandboxWorkspaceError("副本控制清单格式无效")
        kind = raw["kind"]
        digest = raw["digest"]
        integer_values = [raw[name] for name in ("mode", "size", "mtime_ns", "device", "inode")]
        if kind not in {"file", "directory"} or any(type(value) is not int for value in integer_values):
            raise SandboxWorkspaceError("副本控制清单字段无效")
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise SandboxWorkspaceError("副本控制清单摘要无效")
        if kind == "file" and digest is None:
            raise SandboxWorkspaceError("文件清单缺少摘要")
        if kind == "directory" and digest is not None:
            raise SandboxWorkspaceError("目录清单不能包含摘要")
        return cls(kind, *integer_values, digest)


class SandboxWorkspace:
    """绑定原项目、Session、generation、执行副本和不可变基线。"""

    def __init__(
        self,
        *,
        original_workspace: Path,
        execution_workspace: Path,
        control_path: Path,
        session_id: str,
        generation: int,
        baseline: dict[str, WorkspaceEntry],
        excluded_paths: tuple[str, ...],
        budgets: CopyBudgets,
        resumed: bool,
        adopted: bool = False,
    ) -> None:
        self.original_workspace = original_workspace
        self.execution_workspace = execution_workspace
        self.control_path = control_path
        self.session_id = session_id
        self.generation = generation
        self.baseline = MappingProxyType(dict(baseline))
        self.excluded_paths = excluded_paths
        self.budgets = budgets
        self.resumed = resumed
        self.adopted = adopted
        # 文件工具、命令、扫描和发布必须使用同一把锁串行化。
        self.operation_lock = threading.RLock()

    @classmethod
    def prepare(
        cls,
        original_workspace: Path,
        runtime_root: Path,
        *,
        session_id: str,
        generation: int,
        budgets: CopyBudgets | None = None,
        cancellation: CancellationToken | None = None,
    ) -> "SandboxWorkspace":
        """准备新副本，或安全恢复同一 Session/generation 的完整草稿。"""

        if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
            raise SandboxWorkspaceError("Session ID 不能安全用于副本路径")
        if type(generation) is not int or generation < 0:
            raise SandboxWorkspaceError("generation 必须是非负整数")
        limits = budgets or CopyBudgets()
        source = Path(original_workspace).resolve()
        if not source.is_dir() or _is_link_or_reparse(source):
            raise SandboxWorkspaceError("原工作区不存在或不是可信普通目录")
        state_root = Path(runtime_root).resolve()
        generation_root = state_root / session_id / str(generation)
        execution = generation_root / "workspace"
        control = generation_root / "control.json"
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        if generation_root.exists():
            return cls._resume(
                source,
                generation_root,
                execution,
                control,
                session_id=session_id,
                generation=generation,
                budgets=limits,
            )

        created = False
        try:
            generation_root.mkdir(parents=True, exist_ok=False)
            created = True
            execution.mkdir()
            baseline, excluded = _scan_tree(
                source,
                limits,
                purpose="source",
                cancellation=cancellation,
            )
            _copy_inventory(
                source,
                execution,
                baseline,
                cancellation=cancellation,
            )
            # 第二次完整扫描捕获新增、删除、换 inode、换 mode 和同大小内容替换。
            confirmed, confirmed_excluded = _scan_tree(
                source,
                limits,
                purpose="source",
                cancellation=cancellation,
            )
            if confirmed != baseline or confirmed_excluded != excluded:
                raise SandboxWorkspaceError("复制期间原工作区发生变化，拒绝混合基线")
            copied, _ = _scan_tree(
                execution,
                limits,
                purpose="execution",
                cancellation=cancellation,
            )
            if not _same_copy(baseline, copied):
                raise SandboxWorkspaceError("执行副本与可信基线不一致")
            payload = {
                "version": _CONTROL_VERSION,
                "original_workspace": str(source),
                "session_id": session_id,
                "generation": generation,
                "excluded_paths": list(excluded),
                "budgets": {
                    "max_entries": limits.max_entries,
                    "max_file_bytes": limits.max_file_bytes,
                    "max_total_bytes": limits.max_total_bytes,
                },
                "baseline": {path: entry.to_json() for path, entry in baseline.items()},
            }
            _write_control(control, payload)
            return cls(
                original_workspace=source,
                execution_workspace=execution,
                control_path=control,
                session_id=session_id,
                generation=generation,
                baseline=baseline,
                excluded_paths=excluded,
                budgets=limits,
                resumed=False,
            )
        except BaseException:
            if created:
                _remove_owned_generation(generation_root, state_root)
            raise

    @classmethod
    def _resume(
        cls,
        source: Path,
        generation_root: Path,
        execution: Path,
        control: Path,
        *,
        session_id: str,
        generation: int,
        budgets: CopyBudgets,
    ) -> "SandboxWorkspace":
        if (
            _is_link_or_reparse(generation_root)
            or not execution.is_dir()
            or _is_link_or_reparse(execution)
            or not control.is_file()
            or _is_link_or_reparse(control)
        ):
            raise SandboxWorkspaceError("发现不完整或不可信的既有副本，拒绝覆盖")
        try:
            if control.stat().st_size > _CONTROL_MAX_BYTES:
                raise SandboxWorkspaceError("副本控制清单超限")
            raw = json.loads(control.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SandboxWorkspaceError("副本控制清单无法安全读取") from exc
        required = {
            "version",
            "original_workspace",
            "session_id",
            "generation",
            "excluded_paths",
            "budgets",
            "baseline",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise SandboxWorkspaceError("副本控制清单格式无效")
        if (
            raw["version"] != _CONTROL_VERSION
            or raw["original_workspace"] != str(source)
            or raw["session_id"] != session_id
            or raw["generation"] != generation
        ):
            raise SandboxWorkspaceError("副本控制清单身份不匹配")
        raw_budgets = raw["budgets"]
        expected_budgets = {
            "max_entries": budgets.max_entries,
            "max_file_bytes": budgets.max_file_bytes,
            "max_total_bytes": budgets.max_total_bytes,
        }
        if raw_budgets != expected_budgets:
            raise SandboxWorkspaceError("副本预算与既有控制清单不一致")
        if not isinstance(raw["excluded_paths"], list) or not all(
            isinstance(item, str) for item in raw["excluded_paths"]
        ):
            raise SandboxWorkspaceError("副本排除清单无效")
        if not isinstance(raw["baseline"], dict):
            raise SandboxWorkspaceError("副本基线无效")
        baseline = {
            _validate_relative_path(path): WorkspaceEntry.from_json(value)
            for path, value in raw["baseline"].items()
        }
        # 草稿可恢复，但所有结构仍要重新安全扫描；验证能力由上层保持失效。
        _scan_tree(execution, budgets, purpose="execution")
        return cls(
            original_workspace=source,
            execution_workspace=execution,
            control_path=control,
            session_id=session_id,
            generation=generation,
            baseline=baseline,
            excluded_paths=tuple(raw["excluded_paths"]),
            budgets=budgets,
            resumed=True,
        )

    @classmethod
    def capture_existing(
        cls,
        workspace: Path,
        *,
        session_id: str,
        generation: int = 0,
        budgets: CopyBudgets | None = None,
    ) -> "SandboxWorkspace":
        """绑定调用方已经隔离的临时工作区（仅供 Eval 控制面）。"""

        if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
            raise SandboxWorkspaceError("Session ID 不能安全用于副本身份")
        if type(generation) is not int or generation < 0:
            raise SandboxWorkspaceError("generation 必须是非负整数")
        root = Path(workspace).resolve()
        limits = budgets or CopyBudgets()
        baseline, excluded = _scan_tree(root, limits, purpose="execution")
        return cls(
            original_workspace=root,
            execution_workspace=root,
            control_path=root.parent / f"{root.name}.sandbox-control",
            session_id=session_id,
            generation=generation,
            baseline=baseline,
            excluded_paths=excluded,
            budgets=limits,
            resumed=False,
            adopted=True,
        )

    def current_entries(self) -> dict[str, WorkspaceEntry]:
        with self.operation_lock:
            entries, _ = _scan_tree(
                self.execution_workspace,
                self.budgets,
                purpose="execution",
            )
            return entries

    def original_entries(self) -> dict[str, WorkspaceEntry]:
        """重新扫描原项目；发布预览和提交不能只信任启动时基线。"""

        entries, _ = _scan_tree(
            self.original_workspace,
            self.budgets,
            purpose="source",
        )
        return entries

    def rebase_from_original(self, *, require_execution_match: bool) -> None:
        """发布或发布撤销后，把原项目的已证明版本设为新基线。"""

        with self.operation_lock:
            original = self.original_entries()
            execution = self.current_entries()
            if require_execution_match and not _same_copy(original, execution):
                raise SandboxWorkspaceError("发布后的原项目与执行副本不一致")
            if self.adopted:
                self.baseline = MappingProxyType(dict(original))
                return
            payload = {
                "version": _CONTROL_VERSION,
                "original_workspace": str(self.original_workspace),
                "session_id": self.session_id,
                "generation": self.generation,
                "excluded_paths": list(self.excluded_paths),
                "budgets": {
                    "max_entries": self.budgets.max_entries,
                    "max_file_bytes": self.budgets.max_file_bytes,
                    "max_total_bytes": self.budgets.max_total_bytes,
                },
                "baseline": {
                    path: entry.to_json() for path, entry in original.items()
                },
            }
            _write_control(self.control_path, payload)
            self.baseline = MappingProxyType(dict(original))

    def changed_paths(self) -> tuple[tuple[str, str], ...]:
        """返回 `(状态, 相对路径)`；不会返回源码正文。"""

        current = self.current_entries()
        changes: list[tuple[str, str]] = []
        for path in sorted(set(self.baseline) | set(current)):
            before = self.baseline.get(path)
            after = current.get(path)
            if before is not None and before.kind == "directory" and (
                after is None or after.kind == "directory"
            ):
                continue
            if before is None:
                if after is not None and after.kind == "file":
                    changes.append(("A", path))
            elif after is None:
                if before.kind == "file":
                    changes.append(("D", path))
            elif before.kind != after.kind:
                changes.append(("T", path))
            elif before.kind == "file" and (
                before.digest != after.digest or before.mode != after.mode
            ):
                changes.append(("M", path))
        return tuple(changes)

    def diff_stat(self) -> str:
        changes = self.changed_paths()
        if not changes:
            return "执行副本没有待发布文件变更"
        return "\n".join(f"{status} {path}" for status, path in changes)


def _validate_relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise SandboxWorkspaceError("副本清单包含无效相对路径")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != raw:
        raise SandboxWorkspaceError("副本清单路径越界")
    return raw


def _scan_tree(
    root: Path,
    budgets: CopyBudgets,
    *,
    purpose: Literal["source", "execution"],
    cancellation: CancellationToken | None = None,
) -> tuple[dict[str, WorkspaceEntry], tuple[str, ...]]:
    if not root.is_dir() or _is_link_or_reparse(root):
        raise SandboxWorkspaceError("扫描根目录不可信")
    entries: dict[str, WorkspaceEntry] = {}
    excluded: list[str] = []
    total_bytes = 0

    def walk(directory: Path) -> None:
        nonlocal total_bytes
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SandboxWorkspaceError("无法完整扫描工作区") from exc
        for child in children:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if _excluded(relative, purpose=purpose):
                excluded.append(relative)
                continue
            if purpose == "execution" and _forbidden_execution_path(relative):
                raise SandboxWorkspaceError(f"执行副本包含不支持的敏感路径：{relative}")
            try:
                # Windows 的 ``DirEntry.stat`` 可能把 st_dev/st_ino/st_nlink
                # 返回为 0，而同一路径的句柄 fstat 提供真实身份。这里使用
                # Path.stat，后续仍以打开句柄再次核对，避免把平台差异误判
                # 为复制竞争。
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SandboxWorkspaceError("无法读取工作区条目元数据") from exc
            if child.is_symlink() or _metadata_is_reparse(metadata):
                raise SandboxWorkspaceError(f"工作区包含链接或 reparse point：{relative}")
            if len(entries) >= budgets.max_entries:
                raise SandboxWorkspaceError("工作区条目数量超过复制预算")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                entries[relative] = WorkspaceEntry(
                    "directory",
                    mode,
                    0,
                    metadata.st_mtime_ns,
                    metadata.st_dev,
                    metadata.st_ino,
                )
                walk(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SandboxWorkspaceError(f"工作区包含特殊文件：{relative}")
            if metadata.st_nlink > 1:
                raise SandboxWorkspaceError(f"工作区包含硬链接文件：{relative}")
            if metadata.st_size > budgets.max_file_bytes:
                raise SandboxWorkspaceError(f"单文件超过复制预算：{relative}")
            total_bytes += metadata.st_size
            if total_bytes > budgets.max_total_bytes:
                raise SandboxWorkspaceError("工作区总大小超过复制预算")
            digest = _hash_regular_file(path, metadata, cancellation=cancellation)
            entries[relative] = WorkspaceEntry(
                "file",
                mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_dev,
                metadata.st_ino,
                digest,
            )

    walk(root)
    return entries, tuple(sorted(excluded))


def _excluded(relative: str, *, purpose: Literal["source", "execution"]) -> bool:
    path = PurePosixPath(relative)
    lowered = tuple(part.lower() for part in path.parts)
    if purpose == "execution":
        return any(part in _SAFE_RUNTIME_OUTPUT_PARTS for part in lowered) or (
            len(lowered) == 1 and lowered[0] in _SAFE_RUNTIME_OUTPUT_FILES
        )
    name = lowered[-1]
    if name.startswith(".env") or is_sensitive_workspace_path(relative):
        return True
    if any(part in _SOURCE_EXCLUDED_PARTS for part in lowered):
        return True
    if name in {"sessions.db", "sessions.db-wal", "sessions.db-shm"}:
        return True
    return False


def _forbidden_execution_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    lowered = tuple(part.lower() for part in path.parts)
    name = lowered[-1]
    return (
        name.startswith(".env")
        or is_sensitive_workspace_path(relative)
        or any(part in _SOURCE_EXCLUDED_PARTS for part in lowered)
        or name in {"sessions.db", "sessions.db-wal", "sessions.db-shm"}
    )


def _hash_regular_file(
    path: Path,
    expected: os.stat_result,
    *,
    cancellation: CancellationToken | None = None,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SandboxWorkspaceError("无法安全打开普通文件") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not _same_identity_and_version(expected, opened):
            raise SandboxWorkspaceError("文件在打开前发生变化")
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not _same_identity_and_version(opened, after):
            raise SandboxWorkspaceError("文件在读取期间发生变化")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _copy_inventory(
    source: Path,
    destination: Path,
    inventory: dict[str, WorkspaceEntry],
    *,
    cancellation: CancellationToken | None,
) -> None:
    for relative, entry in sorted(
        inventory.items(),
        key=lambda item: (len(PurePosixPath(item[0]).parts), item[0]),
    ):
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        target = destination / Path(*PurePosixPath(relative).parts)
        if entry.kind == "directory":
            target.mkdir()
            _safe_chmod(target, entry.mode)
            continue
        source_path = source / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = source_path.stat(follow_symlinks=False)
        if not _entry_matches_metadata(entry, expected):
            raise SandboxWorkspaceError("复制前文件版本不再匹配基线")
        source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
        )
        try:
            source_fd = os.open(source_path, source_flags)
            target_fd = os.open(target, destination_flags, 0o600)
        except OSError as exc:
            raise SandboxWorkspaceError("无法安全创建执行副本文件") from exc
        digest = hashlib.sha256()
        try:
            opened = os.fstat(source_fd)
            if not _entry_matches_metadata(entry, opened):
                raise SandboxWorkspaceError("复制打开的源文件版本不匹配")
            while True:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                chunk = os.read(source_fd, 64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
            if not _entry_matches_metadata(entry, os.fstat(source_fd)):
                raise SandboxWorkspaceError("源文件在复制期间发生变化")
            os.fsync(target_fd)
        finally:
            os.close(source_fd)
            os.close(target_fd)
        if digest.hexdigest() != entry.digest:
            raise SandboxWorkspaceError("复制内容与可信基线不一致")
        _safe_chmod(target, entry.mode)


def _same_copy(
    source: dict[str, WorkspaceEntry],
    copied: dict[str, WorkspaceEntry],
) -> bool:
    if set(source) != set(copied):
        return False
    for path, before in source.items():
        after = copied[path]
        if (
            before.kind != after.kind
            or before.mode != after.mode
            or before.size != after.size
            or before.digest != after.digest
        ):
            return False
    return True


def _entry_matches_metadata(entry: WorkspaceEntry, metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not _metadata_is_reparse(metadata)
        and metadata.st_nlink == 1
        and entry.mode == stat.S_IMODE(metadata.st_mode)
        and entry.size == metadata.st_size
        and entry.mtime_ns == metadata.st_mtime_ns
        and entry.device == metadata.st_dev
        and entry.inode == metadata.st_ino
    )


def _same_identity_and_version(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and second.st_nlink == 1
        and stat.S_ISREG(second.st_mode)
        and not _metadata_is_reparse(second)
    )


def _metadata_is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(metadata.st_mode) or _metadata_is_reparse(metadata)


def _safe_chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode & 0o777)
    except OSError as exc:
        raise SandboxWorkspaceError("无法复制文件权限模式") from exc


def _write_control(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name("control.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _CONTROL_MAX_BYTES:
        raise SandboxWorkspaceError("副本控制清单超过大小限制")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise SandboxWorkspaceError("无法原子写入副本控制清单") from exc


def _remove_owned_generation(generation_root: Path, state_root: Path) -> None:
    resolved_parent = generation_root.parent.resolve()
    if not resolved_parent.is_relative_to(state_root) or generation_root.name in {"", ".", ".."}:
        raise SandboxWorkspaceError("拒绝清理边界不明的副本目录")
    shutil.rmtree(generation_root)
