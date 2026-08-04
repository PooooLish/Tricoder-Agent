"""经审批的写入工具：编辑、创建与应用受限补丁。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tricoder.changes import (
    FileChange,
    FileIdentity,
    FileSnapshot,
    render_file_diff,
)
from tricoder.models import ToolResult
from tricoder.patches import (
    FilePatch,
    PatchError,
    apply_file_patch,
    parse_unified_diff,
)
from tricoder.policy import PolicyError

from tricoder.tools.binding import _DirectoryBinding
from tricoder.tools.handlers import ToolHandler


@dataclass(frozen=True, slots=True)
class _PreparedFilePatch:
    """审批前完全计算好的单文件补丁，不保留待执行的源码操作。"""

    patch: FilePatch
    path: Path
    relative_path: str
    before: FileSnapshot | None
    after_content: str
    mode: int


@dataclass(frozen=True, slots=True)
class _CommittedFilePatch:
    """已经发布到目标路径、可用于安全补偿核验的真实后态。"""

    prepared: _PreparedFilePatch
    after: FileSnapshot


class EditFileTool(ToolHandler):
    name = "edit_file"
    description = "经审批后精确替换工作区内文件的一段文本。"
    parameters = ToolHandler._schema(
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        ["path", "old_text", "new_text"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止编辑文件")
        raw_path = self._required_str(arguments, "path")
        path = self.context.workspace_policy.resolve_path(raw_path)
        if not path.is_file():
            return ToolResult(False, "edit_file 的目标必须是文件")
        old_text = self._required_str(arguments, "old_text")
        new_text = self._required_str(arguments, "new_text", allow_empty=True)
        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            path.parent,
        )
        temporary_name: str | None = None
        committed = False
        close_warning = False
        journal_warning = False
        try:
            preapproved = self.context.workspace_policy.resolve_path(raw_path)
            if preapproved != path or not binding.verify_parent(preapproved.parent):
                return ToolResult(False, "目标父目录身份发生变化，拒绝请求审批")
            relative = path.relative_to(self.context.workspace_policy.workspace)
            relative_path = relative.as_posix()
            before = self._snapshot(binding, path.name, relative_path)
            if not self._journal_before_is_continuous(relative_path, before):
                return ToolResult(False, f"任务内文件状态不连续：{relative_path}")
            original = before.content
            if original.count(old_text) != 1:
                return ToolResult(
                    False,
                    "old_text 必须在目标文件中恰好出现一次，请重新读取文件",
                )
            updated = original.replace(old_text, new_text, 1)
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=str(relative),
                    tofile=str(relative),
                )
            )
            projected_after = FileSnapshot(
                relative_path,
                updated,
                before.mode,
                before.identity,
            )
            self._reserve_change(FileChange(relative_path, before, projected_after))
            if not self.context.approver("edit_file", diff):
                return ToolResult(False, "用户拒绝了文件修改")

            verified = self.context.workspace_policy.resolve_path(raw_path)
            if not binding.verify_parent(verified.parent) or verified != path:
                return ToolResult(False, "审批后目标父目录身份发生变化，拒绝写入")
            current = self._snapshot(binding, path.name, relative_path)
            if current.identity != before.identity or current.content != original:
                return ToolResult(False, "审批后目标文件发生变化，请重新读取并审批")

            temporary_name = binding.create_temporary(
                path.name,
                updated,
                before.mode,
            )
            published_after = self._snapshot(binding, temporary_name, relative_path)
            binding.replace(temporary_name, path.name)
            committed = True
            temporary_name = None
            try:
                after = self._snapshot(binding, path.name, relative_path)
            except Exception:
                self._record_unverified_expected(
                    relative_path,
                    before,
                    published_after,
                )
                return ToolResult(
                    False,
                    f"文件发布后状态无法验证：{relative_path}",
                )
            if after != published_after:
                self._mark_journal_tainted(relative_path)
                return ToolResult(
                    False,
                    f"文件发布后状态无法验证：{relative_path}",
                )
            try:
                self._record_committed(relative_path, before, after)
            except Exception:
                journal_warning = True
        finally:
            if temporary_name is not None:
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            try:
                binding.close()
            except OSError:
                if not committed:
                    raise
                close_warning = True
        output = f"已修改 {relative}"
        if journal_warning:
            output += "；账本警告：提交后快照或记录失败，文件修改已提交，请在验证时检查路径"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，文件修改已提交，请在验证时检查目录"
        return ToolResult(True, output, relative_path)


class CreateFileTool(ToolHandler):
    name = "create_file"
    description = "经审批后在工作区内创建新的 UTF-8 文件。"
    parameters = ToolHandler._schema(
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """经审批后，以不可覆盖的原子发布方式创建 UTF-8 文件。"""
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止创建文件")
        raw_path = self._required_str(arguments, "path")
        path = self.context.workspace_policy.resolve_path(
            raw_path,
            must_exist=False,
        )
        content = self._required_str(arguments, "content", allow_empty=True)
        if not path.parent.is_dir():
            return ToolResult(False, "create_file 的父目录必须存在")
        if path.exists():
            return ToolResult(False, "create_file 的目标文件已存在，拒绝覆盖")

        relative = path.relative_to(self.context.workspace_policy.workspace)
        diff = (
            "".join(
                difflib.unified_diff(
                    [],
                    content.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=str(relative),
                )
            )
            if content
            else (
                f"--- /dev/null\n+++ {relative}\n"
                "@@ -0,0 +0,0 @@\n"
                "（创建空文件，内容为 0 字符）\n"
            )
        )

        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            path.parent,
        )
        temporary_name: str | None = None
        committed = False
        cleanup_warning = False
        close_warning = False
        journal_warning = False
        verification_failed = False
        try:
            preapproved = self.context.workspace_policy.resolve_path(
                raw_path,
                must_exist=False,
            )
            if preapproved != path or not binding.verify_parent(preapproved.parent):
                return ToolResult(False, "目标父目录身份发生变化，拒绝请求审批")
            relative_path = relative.as_posix()
            if not self._journal_before_is_continuous(relative_path, None):
                return ToolResult(False, f"任务内文件状态不连续：{relative_path}")
            projected_after = FileSnapshot(
                relative_path,
                content,
                0o600,
                FileIdentity(0, 0),
            )
            self._reserve_change(FileChange(relative_path, None, projected_after))
            if not self.context.approver("create_file", diff):
                return ToolResult(False, "用户拒绝了创建文件")

            verified = self.context.workspace_policy.resolve_path(
                raw_path,
                must_exist=False,
            )
            if not binding.verify_parent(verified.parent) or verified != path:
                return ToolResult(False, "审批后目标父目录身份发生变化，拒绝写入")
            if binding.target_exists(path.name):
                return ToolResult(False, "审批后目标文件已存在，拒绝覆盖")

            temporary_name = binding.create_temporary(path.name, content, 0o600)
            published_after = self._snapshot(binding, temporary_name, relative_path)
            try:
                binding.link(temporary_name, path.name)
            except OSError:
                return ToolResult(False, "create_file 无法原子发布文件，拒绝覆盖")
            committed = True
            snapshot_failed = False
            try:
                after = self._snapshot(binding, path.name, relative_path)
            except Exception:
                self._record_unverified_expected(
                    relative_path,
                    None,
                    published_after,
                )
                snapshot_failed = True
                verification_failed = True
            else:
                verification_failed = after != published_after
            if verification_failed and not snapshot_failed:
                self._mark_journal_tainted(relative_path)
            if not verification_failed:
                try:
                    self._record_committed(relative_path, None, after)
                except Exception:
                    journal_warning = True
            try:
                binding.unlink(temporary_name)
            except OSError:
                cleanup_warning = True
            else:
                temporary_name = None
        finally:
            if temporary_name is not None:
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            try:
                binding.close()
            except OSError:
                if not committed:
                    raise
                close_warning = True
        if verification_failed:
            output = f"文件发布后状态无法验证：{relative_path}"
            if cleanup_warning:
                output += "；清理警告：临时链接未能删除，请验证受影响路径"
            if close_warning:
                output += "；关闭警告：目录绑定未能正常关闭，请验证受影响路径"
            return ToolResult(False, output)
        output = f"已创建 {relative}"
        if journal_warning:
            output += "；账本警告：提交后快照或记录失败，文件创建已提交，请在验证时检查路径"
        if cleanup_warning:
            output += "；清理警告：临时链接未能删除，请在验证时检查目录"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，文件创建已提交，请在验证时检查目录"
        return ToolResult(True, output, relative_path)


class ApplyPatchTool(ToolHandler):
    name = "apply_patch"
    description = "经一次审批后原子应用受限的多文件 unified diff。"
    parameters = ToolHandler._schema({"patch": {"type": "string"}}, ["patch"])

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """全量预检后，以一次审批提交受限的多文件补丁。"""

        if self.context.read_only:
            return ToolResult(False, "只读模式禁止应用补丁")
        source = self._required_str(arguments, "patch")
        try:
            file_patches = parse_unified_diff(source)
        except PatchError as exc:
            return ToolResult(False, f"补丁无效：{exc}")

        workspace = self.context.workspace_policy.workspace
        resolved: list[tuple[FilePatch, Path, str]] = []
        seen_targets: set[str] = set()
        for file_patch in file_patches:
            path = self.context.workspace_policy.resolve_path(
                file_patch.path,
                must_exist=not file_patch.create,
            )
            relative_path = path.relative_to(workspace).as_posix()
            if relative_path in seen_targets:
                return ToolResult(False, f"补丁目标重复：{relative_path}")
            seen_targets.add(relative_path)
            if not path.parent.is_dir():
                return ToolResult(False, f"补丁目标父目录不存在：{relative_path}")
            if file_patch.create:
                if path.exists():
                    return ToolResult(False, f"补丁创建目标已存在：{relative_path}")
            elif not path.is_file():
                return ToolResult(False, f"补丁目标不是普通文件：{relative_path}")
            resolved.append((file_patch, path, relative_path))

        bindings: dict[Path, _DirectoryBinding] = {}
        temporary_files: list[tuple[_DirectoryBinding, str]] = []
        committed_paths: list[str] = []
        close_warning = False
        try:
            for parent in sorted({path.parent for _, path, _ in resolved}, key=str):
                bindings[parent] = _DirectoryBinding.open(workspace, parent)

            prepared: list[_PreparedFilePatch] = []
            for file_patch, path, relative_path in resolved:
                binding = bindings[path.parent]
                if not binding.verify_parent(path.parent):
                    return ToolResult(False, f"补丁目标父目录身份已变化：{relative_path}")
                if file_patch.create:
                    if binding.target_exists(path.name):
                        return ToolResult(False, f"补丁创建目标已存在：{relative_path}")
                    before = None
                    original = ""
                    mode = 0o600
                else:
                    before = self._snapshot(binding, path.name, relative_path)
                    original = before.content
                    mode = before.mode
                if not self._journal_before_is_continuous(relative_path, before):
                    return ToolResult(False, f"任务内文件状态不连续：{relative_path}")
                try:
                    after_content = apply_file_patch(original, file_patch)
                except PatchError as exc:
                    return ToolResult(False, f"补丁无法应用到 {relative_path}：{exc}")
                if not file_patch.create and after_content == original:
                    return ToolResult(False, f"补丁对现有文件没有净变化：{relative_path}")
                prepared.append(
                    _PreparedFilePatch(
                        file_patch,
                        path,
                        relative_path,
                        before,
                        after_content,
                        mode,
                    )
                )

            audit_paths = tuple(
                sorted(item.relative_path for item in prepared)
            )
            change_chars = sum(
                len(line[1:])
                for item in prepared
                for hunk in item.patch.hunks
                for line in hunk.lines
                if line.startswith(("+", "-"))
            )
            projected = tuple(
                FileChange(
                    item.relative_path,
                    item.before,
                    FileSnapshot(
                        item.relative_path,
                        item.after_content,
                        item.mode,
                        item.before.identity if item.before else FileIdentity(0, 0),
                    ),
                )
                for item in prepared
            )
            self._reserve_changes(projected)
            approval_detail = "".join(self._render_patch_diff(item) for item in prepared)
            try:
                approved = self.context.approver("apply_patch", approval_detail)
            except (OSError, ValueError, TypeError):
                return ToolResult(
                    False,
                    "补丁审批失败，未执行写入",
                    audit_paths=audit_paths,
                    change_chars=change_chars,
                )
            if not approved:
                return ToolResult(
                    False,
                    "用户拒绝了多文件补丁",
                    audit_paths=audit_paths,
                    change_chars=change_chars,
                )

            for item in prepared:
                verified = self.context.workspace_policy.resolve_path(
                    item.patch.path,
                    must_exist=not item.patch.create,
                )
                binding = bindings[item.path.parent]
                if verified != item.path or not binding.verify_parent(verified.parent):
                    return ToolResult(
                        False,
                        "审批后补丁目标父目录身份发生变化，拒绝写入",
                        audit_paths=audit_paths,
                        change_chars=change_chars,
                    )
                if item.before is None:
                    if binding.target_exists(item.path.name):
                        return ToolResult(
                            False,
                            "审批后补丁创建目标已存在，拒绝覆盖",
                            audit_paths=audit_paths,
                            change_chars=change_chars,
                        )
                else:
                    current = self._snapshot(binding, item.path.name, item.relative_path)
                    if current != item.before:
                        return ToolResult(
                            False,
                            "审批后补丁目标发生变化，拒绝全部写入",
                            audit_paths=audit_paths,
                            change_chars=change_chars,
                        )

            committed: list[_CommittedFilePatch] = []
            failed_path = ""
            try:
                for item in sorted(prepared, key=lambda value: value.relative_path):
                    failed_path = item.relative_path
                    binding = bindings[item.path.parent]
                    temporary_name = binding.create_temporary(
                        item.path.name,
                        item.after_content,
                        item.mode,
                    )
                    temporary_files.append((binding, temporary_name))
                    expected_after = self._snapshot(
                        binding,
                        temporary_name,
                        item.relative_path,
                    )
                    if not self._patch_target_matches_before(item, binding):
                        self._mark_journal_tainted(item.relative_path)
                        raise PolicyError("补丁目标在逐文件发布前发生变化")
                    if item.before is None:
                        binding.link(temporary_name, item.path.name)
                    else:
                        binding.replace(temporary_name, item.path.name)
                        temporary_files.pop()
                    committed_item = _CommittedFilePatch(item, expected_after)
                    committed.append(committed_item)
                    try:
                        after = self._snapshot(binding, item.path.name, item.relative_path)
                    except Exception:
                        self._mark_journal_tainted(item.relative_path)
                        raise
                    if after != expected_after:
                        self._mark_journal_tainted(item.relative_path)
                        raise PolicyError("补丁发布后目标状态不匹配预期后态")
                    self._record_committed(item.relative_path, item.before, after)
                    committed_paths.append(item.relative_path)
                    if item.before is None:
                        binding.unlink(temporary_name)
                        temporary_files.pop()
            except Exception:
                compensation_failures = self._compensate_patch_commits(
                    committed,
                    bindings,
                    temporary_files,
                )
                affected_paths = tuple(
                    dict.fromkeys(
                        [entry.prepared.relative_path for entry in committed]
                        + ([failed_path] if failed_path else [])
                    )
                )
                affected_text = "、".join(affected_paths)
                output = f"补丁提交失败；受影响路径：{affected_text}"
                if compensation_failures:
                    output += f"；补偿未完成：{'、'.join(compensation_failures)}"
                else:
                    output += "；已完成安全补偿"
                return ToolResult(
                    False,
                    output,
                    modified_paths=tuple(compensation_failures),
                    audit_paths=audit_paths,
                    change_chars=change_chars,
                )
        finally:
            for binding, temporary_name in reversed(temporary_files):
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            for binding in reversed(tuple(bindings.values())):
                try:
                    binding.close()
                except OSError:
                    if not committed_paths:
                        raise
                    close_warning = True

        paths_text = "、".join(committed_paths)
        output = f"已应用补丁到 {len(committed_paths)} 个文件：{paths_text}"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，请验证受影响路径"
        return ToolResult(
            True,
            output,
            modified_paths=tuple(committed_paths),
            audit_paths=audit_paths,
            change_chars=change_chars,
        )

    def _patch_target_matches_before(
        self,
        item: _PreparedFilePatch,
        binding: _DirectoryBinding,
    ) -> bool:
        """在每次 link/replace 紧前再次比较完整前态或不存在状态。"""

        try:
            if item.before is None:
                return not binding.target_exists(item.path.name)
            if not binding.target_exists(item.path.name):
                return False
            return (
                self._snapshot(binding, item.path.name, item.relative_path)
                == item.before
            )
        except (OSError, UnicodeError):
            return False

    @staticmethod
    def _render_patch_diff(item: _PreparedFilePatch) -> str:
        """为一次审批渲染完整规范 diff，不复用模型输出上限。"""

        return render_file_diff(
            item.relative_path,
            item.before.content if item.before is not None else None,
            item.after_content,
            before_mode=item.before.mode if item.before is not None else None,
            after_mode=item.mode,
        )

    def _compensate_patch_commits(
        self,
        committed: list[_CommittedFilePatch],
        bindings: dict[Path, _DirectoryBinding],
        temporary_files: list[tuple[_DirectoryBinding, str]],
    ) -> tuple[str, ...]:
        """逆序补偿已发布目标；身份不符时绝不覆盖并保留真实账本状态。"""

        failures: list[str] = []
        for entry in reversed(committed):
            item = entry.prepared
            binding = bindings[item.path.parent]
            try:
                current = self._snapshot(binding, item.path.name, item.relative_path)
                if current != entry.after:
                    raise PolicyError("补偿前目标状态不再匹配已提交后态")
                if item.before is None:
                    binding.unlink(item.path.name)
                    if binding.target_exists(item.path.name):
                        raise OSError("补偿后创建目标仍然存在")
                    self._record_committed(item.relative_path, entry.after, None)
                    continue

                temporary_name = binding.create_temporary(
                    item.path.name,
                    item.before.content,
                    item.before.mode,
                )
                temporary_files.append((binding, temporary_name))
                binding.replace(temporary_name, item.path.name)
                temporary_files.pop()
                restored = self._snapshot(binding, item.path.name, item.relative_path)
                if (
                    restored.content != item.before.content
                    or restored.mode != item.before.mode
                ):
                    raise OSError("补偿后的目标内容或权限不匹配原始快照")
                self._record_committed(item.relative_path, entry.after, restored)
            except Exception:
                failures.append(item.relative_path)
                self._record_actual_patch_state(entry, binding)
        return tuple(sorted(failures))

    def _record_actual_patch_state(
        self,
        entry: _CommittedFilePatch,
        binding: _DirectoryBinding,
    ) -> None:
        """补偿失败后只记录可证明的工具状态；外部状态仅标记冲突。"""

        item = entry.prepared
        try:
            actual = (
                self._snapshot(binding, item.path.name, item.relative_path)
                if binding.target_exists(item.path.name)
                else None
            )
            if actual == entry.after:
                return
            if actual is None and item.before is None:
                self._record_committed(item.relative_path, entry.after, None)
                return
        except Exception:
            pass
        self._mark_journal_tainted(item.relative_path)
