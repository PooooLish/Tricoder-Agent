"""受控目录创建：规划、一次审批、逐层绑定与安全补偿。"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from tricoder.changes import ChangeJournal, DirectorySnapshot
from tricoder.core.cancellation import check_current_cancellation
from tricoder.execution_state import ErrorCode
from tricoder.models import ToolResult, tool_failure
from tricoder.policy import PolicyError, WorkspacePolicy
from tricoder.tools.binding import _DirectoryBinding
from tricoder.tools.handlers import InvalidToolArgument, ToolHandler


MAX_CREATE_DEPTH = 16
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class DirectoryCreationPlan:
    """审批前得到的规范目标与缺失目录链。"""

    target: Path
    relative_target: str
    existing_parent: Path
    missing: tuple[Path, ...]
    missing_relative: tuple[str, ...]
    already_exists: bool


@dataclass(frozen=True, slots=True)
class CreatedDirectory:
    """本次操作确实创建并已核验身份的目录。"""

    path: Path
    snapshot: DirectorySnapshot


_directory_operation_journal: ContextVar[ChangeJournal | None] = ContextVar(
    "directory_operation_journal", default=None
)


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _validate_requested_relative_path(raw_path: str) -> Path:
    if (
        not isinstance(raw_path, str)
        or not raw_path.strip()
        or raw_path != raw_path.strip()
    ):
        raise InvalidToolArgument("path 必须是非空相对路径")
    candidate = Path(raw_path)
    if candidate.is_absolute() or candidate.drive:
        raise InvalidToolArgument("目录目标必须是工作区相对路径")
    lexical_parts = raw_path.replace("\\", "/").split("/")
    raw_parts = candidate.parts
    if (
        not raw_parts
        or any(part in {"", ".", ".."} for part in lexical_parts)
        or any(not all(character.isprintable() for character in part) for part in lexical_parts)
    ):
        raise InvalidToolArgument("目录目标不能是工作区根目录或包含点路径")
    return candidate


def plan_directory_creation(
    policy: WorkspacePolicy,
    raw_path: str,
    *,
    parents: bool,
    exist_ok: bool,
) -> DirectoryCreationPlan:
    """在零写入阶段解析目录链，并拒绝链接、冲突与超深创建。"""

    candidate = _validate_requested_relative_path(raw_path)
    target = policy.resolve_path(candidate, must_exist=False)
    workspace = policy.workspace
    relative = target.relative_to(workspace).as_posix()

    current = workspace
    missing: list[Path] = []
    for part in candidate.parts:
        current = current / part
        if missing:
            missing.append(current)
            continue
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            missing.append(current)
            continue
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise InvalidToolArgument("目录路径与文件、链接或 reparse point 冲突")

    already_exists = not missing
    if already_exists and not exist_ok:
        raise InvalidToolArgument("目标目录已存在且 exist_ok=false")
    if missing and not parents and missing[0] != target:
        raise InvalidToolArgument("父目录不存在且 parents=false")
    if len(missing) > MAX_CREATE_DEPTH:
        raise InvalidToolArgument("单次创建目录层级超过 16 层")
    missing_relative = tuple(
        path.relative_to(workspace).as_posix() for path in missing
    )
    return DirectoryCreationPlan(
        target,
        relative,
        missing[0].parent if missing else target,
        tuple(missing),
        missing_relative,
        already_exists,
    )


def render_directory_approval(paths: tuple[str, ...]) -> str:
    """以字面规范路径展示本次实际拟新增的目录清单。"""

    return "将创建目录：\n" + "".join(f"+ {path}/\n" for path in paths)


def revalidate_directory_plan(
    policy: WorkspacePolicy,
    raw_path: str,
    original: DirectoryCreationPlan,
    *,
    parents: bool,
    exist_ok: bool,
) -> DirectoryCreationPlan:
    """审批后重新规划；任何身份/存在性变化都使原确认失效。"""

    current = plan_directory_creation(
        policy,
        raw_path,
        parents=parents,
        exist_ok=exist_ok,
    )
    if (
        current.target != original.target
        or current.missing != original.missing
        or current.already_exists != original.already_exists
    ):
        raise PolicyError("审批后目录状态发生变化，请重新确认")
    return current


def create_planned_directories(
    handler: ToolHandler,
    plan: DirectoryCreationPlan,
    initial_binding: _DirectoryBinding,
) -> tuple[tuple[CreatedDirectory, ...], _DirectoryBinding]:
    """逐层创建并记录；失败时只逆序删除本次身份匹配的空目录。"""

    workspace = handler.context.workspace_policy.workspace
    created: list[CreatedDirectory] = []
    uncertain_path: str | None = None
    current_binding: _DirectoryBinding | None = initial_binding
    try:
        for path, relative in zip(plan.missing, plan.missing_relative):
            check_current_cancellation()
            parent = path.parent
            if (
                current_binding is None
                or current_binding.parent != parent
                or not current_binding.verify_parent(parent)
            ):
                raise PolicyError("目录父路径身份发生变化")
            # mkdir 抛错本身不能证明目标未出现；先把路径置于保守观察范围，
            # 只有身份与账本提交都完成后才清除。
            uncertain_path = relative
            identity = current_binding.create_directory(path.name)
            verified_identity, mode = current_binding.directory_status(path.name)
            if verified_identity != identity:
                raise PolicyError("新目录身份核验失败")
            snapshot = DirectorySnapshot(relative, mode, identity)
            created.append(CreatedDirectory(path, snapshot))
            handler._record_directory_committed(relative, None, snapshot)
            uncertain_path = None

            child_binding = current_binding.open_child_directory(
                path.name,
                identity,
            )
            if not child_binding.verify_parent(path):
                child_binding.close()
                raise PolicyError("新目录路径身份发生变化")
            current_binding.close()
            current_binding = child_binding
        assert current_binding is not None
        return tuple(created), current_binding
    except BaseException:
        if current_binding is not None:
            try:
                current_binding.close()
            except OSError:
                if created:
                    handler._mark_directory_journal_tainted(
                        created[-1].snapshot.path
                    )
        current_binding = None
        if uncertain_path is not None:
            handler._mark_directory_journal_tainted(uncertain_path)
        for entry in reversed(created):
            binding = None
            try:
                binding = _DirectoryBinding.open(workspace, entry.path.parent)
                binding.remove_empty_directory(
                    entry.path.name,
                    entry.snapshot.identity,
                )
                handler._record_directory_committed(
                    entry.snapshot.path,
                    entry.snapshot,
                    None,
                )
            except BaseException:
                handler._mark_directory_journal_tainted(entry.snapshot.path)
            finally:
                if binding is not None:
                    try:
                        binding.close()
                    except OSError:
                        handler._mark_directory_journal_tainted(
                            entry.snapshot.path
                        )
        raise


def compensate_created_directories(
    handler: ToolHandler,
    created: tuple[CreatedDirectory, ...],
) -> tuple[str, ...]:
    """逆序移除仍为空且身份匹配的本次目录，并返回无法补偿的路径。"""

    workspace = handler.context.workspace_policy.workspace
    failures: list[str] = []
    for entry in reversed(created):
        binding = None
        try:
            binding = _DirectoryBinding.open(workspace, entry.path.parent)
            binding.remove_empty_directory(
                entry.path.name,
                entry.snapshot.identity,
            )
            handler._record_directory_committed(
                entry.snapshot.path,
                entry.snapshot,
                None,
            )
        except BaseException:
            failures.append(entry.snapshot.path)
            handler._mark_directory_journal_tainted(entry.snapshot.path)
        finally:
            if binding is not None:
                try:
                    binding.close()
                except OSError:
                    if entry.snapshot.path not in failures:
                        failures.append(entry.snapshot.path)
                    handler._mark_directory_journal_tainted(entry.snapshot.path)
    return tuple(sorted(failures))


class CreateDirectoryTool(ToolHandler):
    name = "create_directory"
    description = "经审批后在工作区内创建一个或有限层级的普通目录。"
    parameters = ToolHandler._schema(
        {
            "path": {"type": "string"},
            "parents": {"type": "boolean"},
            "exist_ok": {"type": "boolean"},
        },
        ["path"],
    )

    @contextmanager
    def collect_effects(self) -> Iterator[ChangeJournal]:
        journal = ChangeJournal()
        journal.begin_task((), "未运行")
        token = _directory_operation_journal.set(journal)
        try:
            yield journal
        finally:
            _directory_operation_journal.reset(token)

    def _reserve_directories(self, paths: tuple[str, ...]) -> None:
        operation = _directory_operation_journal.get()
        if operation is not None:
            operation.reserve_directories(paths)
        super()._reserve_directories(paths)

    def _record_directory_committed(
        self,
        path: str,
        before: DirectorySnapshot | None,
        after: DirectorySnapshot | None,
    ) -> None:
        operation = _directory_operation_journal.get()
        if operation is not None:
            operation.record_directory_committed(path, before, after)
        super()._record_directory_committed(path, before, after)

    def _mark_directory_journal_tainted(self, path: str) -> None:
        operation = _directory_operation_journal.get()
        if operation is not None:
            operation.mark_directory_tainted(path)
        super()._mark_directory_journal_tainted(path)

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return tool_failure(ErrorCode.POLICY_DENIED, "只读模式禁止创建目录")
        raw_path = self._required_str(arguments, "path")
        parents = arguments.get("parents", True)
        exist_ok = arguments.get("exist_ok", True)
        plan = plan_directory_creation(
            self.context.workspace_policy,
            raw_path,
            parents=parents,
            exist_ok=exist_ok,
        )
        if plan.already_exists:
            return ToolResult(True, f"目录已存在，未修改：{plan.relative_target}")
        self._reserve_directories(plan.missing_relative)
        detail = render_directory_approval(plan.missing_relative)
        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            plan.existing_parent,
        )
        final_binding: _DirectoryBinding | None = None
        try:
            if not binding.verify_parent(plan.existing_parent):
                return tool_failure(
                    ErrorCode.POLICY_DENIED,
                    "目录安全祖先身份发生变化，拒绝请求审批",
                )
            if not self._approve(self.name, detail):
                return tool_failure(ErrorCode.APPROVAL_DENIED, "用户拒绝了目录创建")
            if not binding.verify_parent(plan.existing_parent):
                return tool_failure(
                    ErrorCode.POLICY_DENIED,
                    "审批后目录安全祖先身份发生变化，拒绝写入",
                )
            revalidate_directory_plan(
                self.context.workspace_policy,
                raw_path,
                plan,
                parents=parents,
                exist_ok=exist_ok,
            )
            owned_binding = binding
            binding = None
            created, final_binding = create_planned_directories(
                self,
                plan,
                owned_binding,
            )
        finally:
            if binding is not None:
                binding.close()
            if final_binding is not None:
                final_binding.close()
        paths = tuple(entry.snapshot.path for entry in created)
        return ToolResult(
            True,
            f"已创建 {len(paths)} 个目录：{'、'.join(paths)}",
            audit_paths=paths,
        )
