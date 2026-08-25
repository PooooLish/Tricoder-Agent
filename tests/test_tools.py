import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder import tools as tools_module
from tricoder.changes import (
    ChangeJournal,
    ChangeJournalError,
    FileChange,
    FileIdentity,
    FileSnapshot,
    TaskChangeSet,
    render_change_set_diff,
)
from tricoder.models import ToolDefinition
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools import binding as binding_module
from tricoder.tools import command as command_module
from tricoder.tools import write as write_module
from tricoder.tools.handlers import ToolHandler


class CloseFailingBinding:
    """先真实关闭目录绑定，再模拟关闭 API 报错。"""

    def __init__(self, binding: object) -> None:
        self.binding = binding

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def close(self) -> None:
        self.binding.close()  # type: ignore[attr-defined]
        raise OSError("simulated binding close failure")


def patch_binding_close_failure() -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_failing_close(workspace: Path, parent: Path) -> CloseFailingBinding:
        return CloseFailingBinding(real_open(workspace, parent))

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_failing_close,
    )


class CleanupFailingBinding:
    """保留真实目录绑定与发布，仅模拟提交后的临时文件清理失败。"""

    def __init__(self, binding: object, target: Path) -> None:
        self.binding = binding
        self.target = target

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def unlink(self, temporary_name: str) -> None:
        if self.target.exists():
            raise PermissionError("simulated cleanup failure")
        self.binding.unlink(temporary_name)  # type: ignore[attr-defined]


def patch_binding_cleanup_failure(target: Path) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_failing_cleanup(
        workspace: Path,
        parent: Path,
    ) -> CleanupFailingBinding:
        return CleanupFailingBinding(real_open(workspace, parent), target)

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_failing_cleanup,
    )


class PublishFailingBinding:
    """保留真实发布，仅在第二目标及可选回滚发布点注入失败。"""

    def __init__(self, binding: object, *, rollback_fails: bool) -> None:
        self.binding = binding
        self.rollback_fails = rollback_fails
        self.app_publish_count = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def replace(self, temporary_name: str, target_name: str) -> None:
        if target_name == "other.py":
            raise OSError("PUBLISH-SECOND-SENTINEL")
        if target_name == "app.py":
            self.app_publish_count += 1
            if self.rollback_fails and self.app_publish_count > 1:
                raise OSError("ROLLBACK-FIRST-SENTINEL")
        self.binding.replace(temporary_name, target_name)  # type: ignore[attr-defined]


def patch_binding_publish_failure(*, rollback_fails: bool = False) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_publish_failure(
        workspace: Path,
        parent: Path,
    ) -> PublishFailingBinding:
        return PublishFailingBinding(
            real_open(workspace, parent),
            rollback_fails=rollback_fails,
        )

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_publish_failure,
    )


class NewFileCompensationProbeBinding(PublishFailingBinding):
    """要求补偿删除新建目标前刚通过绑定读取该目标快照。"""

    def __init__(self, binding: object, state: dict[str, object]) -> None:
        super().__init__(binding, rollback_fails=False)
        self.state = state
        self.last_read_name = ""

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        result = self.binding.read_text(name)  # type: ignore[attr-defined]
        self.last_read_name = name
        return result

    def unlink(self, name: str) -> None:
        if name == "aaa-new.py":
            if self.last_read_name != name:
                raise AssertionError("新建目标未经绑定快照核验即删除")
            verified_deletes = self.state.setdefault("verified_deletes", [])
            assert isinstance(verified_deletes, list)
            verified_deletes.append(name)
        self.binding.unlink(name)  # type: ignore[attr-defined]


def patch_new_file_compensation_probe(
    state: dict[str, object],
) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_probe(workspace: Path, parent: Path) -> NewFileCompensationProbeBinding:
        return NewFileCompensationProbeBinding(real_open(workspace, parent), state)

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_probe,
    )


class ExternalReplacementBinding:
    """第二目标发布失败前，以新 inode 外部替换已经提交的首目标。"""

    def __init__(self, binding: object, state: dict[str, object]) -> None:
        self.binding = binding
        self.state = state

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def replace(self, temporary_name: str, target_name: str) -> None:
        if target_name == "other.py":
            parent = self.binding.parent  # type: ignore[attr-defined]
            external = parent / ".external-replacement.tmp"
            external.write_text("external = True\n", encoding="utf-8")
            os.replace(external, parent / "app.py")
            self.state["external_replaced"] = True
            raise OSError("PUBLISH-AFTER-EXTERNAL-REPLACE")
        if target_name == "app.py" and self.state.get("external_replaced"):
            self.state["unsafe_overwrite"] = True
        self.binding.replace(temporary_name, target_name)  # type: ignore[attr-defined]


def patch_external_replacement_before_compensation(
    state: dict[str, object],
) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_external_replace(
        workspace: Path,
        parent: Path,
    ) -> ExternalReplacementBinding:
        return ExternalReplacementBinding(real_open(workspace, parent), state)

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_external_replace,
    )


class LateCommitRaceBinding:
    """在全量复核后，于发布前或发布后注入外部 identity 替换。"""

    def __init__(self, binding: object, *, stage: str, target: str, content: str) -> None:
        self.binding = binding
        self.stage = stage
        self.target = target
        self.content = content
        self.replaced = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def _replace_external(self, target_name: str) -> None:
        parent = self.binding.parent  # type: ignore[attr-defined]
        external = parent / f".{target_name}.late-race-external.tmp"
        external.write_text(self.content, encoding="utf-8")
        os.replace(external, parent / target_name)
        self.replaced = True

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        temporary_name = self.binding.create_temporary(  # type: ignore[attr-defined]
            target_name,
            content,
            mode,
        )
        if self.stage == "before" and target_name == self.target and not self.replaced:
            self._replace_external(target_name)
        return temporary_name

    def replace(self, temporary_name: str, target_name: str) -> None:
        self.binding.replace(temporary_name, target_name)  # type: ignore[attr-defined]
        if self.stage == "after" and target_name == self.target and not self.replaced:
            self._replace_external(target_name)

    def link(self, source_name: str, target_name: str) -> None:
        self.binding.link(source_name, target_name)  # type: ignore[attr-defined]
        if self.stage == "after" and target_name == self.target and not self.replaced:
            self._replace_external(target_name)


def patch_late_commit_race(*, stage: str, target: str, content: str) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_late_race(workspace: Path, parent: Path) -> LateCommitRaceBinding:
        return LateCommitRaceBinding(
            real_open(workspace, parent),
            stage=stage,
            target=target,
            content=content,
        )

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_late_race,
    )


class PostChmodReadFailingBinding:
    """允许权限变更落盘，随后仅让第一次快照读取失败。"""

    def __init__(self, binding: object) -> None:
        self.binding = binding
        self.fail_next_read = False
        self.has_failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def chmod(self, name: str, mode: int) -> None:
        self.binding.chmod(name, mode)  # type: ignore[attr-defined]
        if not self.has_failed:
            self.fail_next_read = True

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        if self.fail_next_read:
            self.fail_next_read = False
            self.has_failed = True
            raise OSError("POST-CHMOD-READ-SENTINEL")
        return self.binding.read_text(name)  # type: ignore[attr-defined,no-any-return]


def patch_post_chmod_read_failure() -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_read_failure(
        workspace: Path,
        parent: Path,
    ) -> PostChmodReadFailingBinding:
        return PostChmodReadFailingBinding(real_open(workspace, parent))

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_read_failure,
    )


class PosixSemanticsBinding:
    """在 Windows 测试机上模拟 POSIX rename/unlink 不要求解除只读。"""

    def __init__(self, binding: object) -> None:
        self.binding = binding

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def chmod(self, _name: str, _mode: int) -> None:
        raise AssertionError("POSIX undo 不应为 rename/unlink 主动 chmod")

    def replace(self, temporary_name: str, target_name: str) -> None:
        if self.binding.target_exists(target_name):  # type: ignore[attr-defined]
            self.binding.chmod(target_name, 0o600)  # type: ignore[attr-defined]
        self.binding.replace(temporary_name, target_name)  # type: ignore[attr-defined]

    def unlink(self, name: str) -> None:
        if self.binding.target_exists(name):  # type: ignore[attr-defined]
            self.binding.chmod(name, 0o600)  # type: ignore[attr-defined]
        self.binding.unlink(name)  # type: ignore[attr-defined]


def patch_posix_semantics_binding() -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_posix_semantics(workspace: Path, parent: Path) -> object:
        # Windows 需要测试替身模拟 POSIX 的 rename/unlink 语义；Linux/macOS
        # 已经具备真实的 POSIX 目录句柄，直接复用生产实现即可。
        if os.name == "nt":
            return PosixSemanticsBinding(
                tools_module._WindowsDirectoryBinding(workspace, parent)
            )
        return real_open(workspace, parent)

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_posix_semantics,
    )


class ReplaceAfterBackupLinkBinding:
    """在 after 硬链接建立后、解锁前注入外部 identity 替换。"""

    def __init__(
        self,
        binding: object,
        replacement: Path,
        backup_mode: int,
        external_mode: int,
    ) -> None:
        self.binding = binding
        self.replacement = replacement
        self.backup_mode = backup_mode
        self.external_mode = external_mode
        self.replaced = False
        self.chmod_names: list[str] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def link(self, source_name: str, target_name: str) -> None:
        self.binding.link(source_name, target_name)  # type: ignore[attr-defined]
        if source_name == "app.py" and target_name.startswith(".app.py."):
            self.binding.chmod(source_name, 0o600)  # type: ignore[attr-defined]
            os.chmod(self.replacement, 0o600)
            os.replace(self.replacement, self.binding.parent / source_name)  # type: ignore[attr-defined]
            self.binding.chmod(target_name, self.backup_mode)  # type: ignore[attr-defined]
            self.binding.chmod(source_name, self.external_mode)  # type: ignore[attr-defined]
            self.replaced = True

    def chmod(self, name: str, mode: int) -> None:
        self.chmod_names.append(name)
        self.binding.chmod(name, mode)  # type: ignore[attr-defined]


def patch_replace_after_backup_link(
    replacement: Path,
    backup_mode: int,
    external_mode: int,
    probes: list[ReplaceAfterBackupLinkBinding],
) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_replacement(
        workspace: Path,
        parent: Path,
    ) -> ReplaceAfterBackupLinkBinding:
        binding = ReplaceAfterBackupLinkBinding(
            real_open(workspace, parent),
            replacement,
            backup_mode,
            external_mode,
        )
        probes.append(binding)
        return binding

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_replacement,
    )


class ReadFailingAfterSuccessfulReplaceBinding:
    """真实发布 before 后，让第一次目标快照读取失败。"""

    def __init__(self, binding: object) -> None:
        self.binding = binding
        self.fail_next_target_read = False
        self.failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def replace(self, temporary_name: str, target_name: str) -> None:
        self.binding.replace(temporary_name, target_name)  # type: ignore[attr-defined]
        if target_name == "app.py" and not self.failed:
            self.fail_next_target_read = True

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        if name == "app.py" and self.fail_next_target_read:
            self.fail_next_target_read = False
            self.failed = True
            raise OSError("POST-REPLACE-READ-SENTINEL")
        return self.binding.read_text(name)  # type: ignore[attr-defined,no-any-return]


def patch_read_failure_after_successful_replace() -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_read_failure(
        workspace: Path,
        parent: Path,
    ) -> ReadFailingAfterSuccessfulReplaceBinding:
        return ReadFailingAfterSuccessfulReplaceBinding(real_open(workspace, parent))

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_read_failure,
    )


class UndoCreatePublishFailureBinding:
    """在 undo 重建已删除文件的 link 前或发布后快照点注入故障。"""

    def __init__(self, binding: object, stage: str) -> None:
        self.binding = binding
        self.stage = stage
        self.fail_next_target_read = False
        self.failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def link(self, source_name: str, target_name: str) -> None:
        if target_name == "deleted.py" and not self.failed:
            if self.stage == "before":
                self.failed = True
                raise OSError("UNDO-CREATE-BEFORE-LINK-SENTINEL")
            self.binding.link(source_name, target_name)  # type: ignore[attr-defined]
            self.fail_next_target_read = True
            return
        self.binding.link(source_name, target_name)  # type: ignore[attr-defined]

    def read_text(self, name: str) -> tuple[str, FileIdentity, int]:
        if name == "deleted.py" and self.fail_next_target_read:
            self.fail_next_target_read = False
            self.failed = True
            raise OSError("UNDO-CREATE-POST-LINK-READ-SENTINEL")
        return self.binding.read_text(name)  # type: ignore[attr-defined,no-any-return]


def patch_undo_create_publish_failure(stage: str) -> object:
    real_open = binding_module._DirectoryBinding.open

    def open_with_publish_failure(
        workspace: Path,
        parent: Path,
    ) -> UndoCreatePublishFailureBinding:
        return UndoCreatePublishFailureBinding(real_open(workspace, parent), stage)

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_publish_failure,
    )


class RecordingApprover:
    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = list(decisions)
        self.requests: list[tuple[str, str]] = []

    def __call__(self, action: str, detail: str) -> bool:
        self.requests.append((action, detail))
        return self.decisions.pop(0)


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "def answer():\n    return 41\n",
            encoding="utf-8",
        )
        self.approver = RecordingApprover([True])
        self.registry = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(self.workspace),
                command_policy=CommandPolicy(),
                approver=self.approver,
                timeout=5,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_definitions_are_stable_read_only_and_match_handler_names(self) -> None:
        """防止公开工具契约与实际可调用工具分叉或被调用方改写。"""
        definitions = self.registry.definitions

        self.assertIsInstance(definitions, tuple)
        self.assertEqual(
            (
                "list_files",
                "read_file",
                "search_text",
                "glob_files",
                "edit_file",
                "create_file",
                "apply_patch",
                "run_command",
                "git_diff",
                "finish",
            ),
            tuple(definition.name for definition in definitions),
        )
        self.assertTrue(all(isinstance(item, ToolDefinition) for item in definitions))
        self.assertEqual(definitions, self.registry.definitions)
        self.assertTrue(all(self.registry.contains(item.name) for item in definitions))
        self.assertFalse(self.registry.contains("unknown_tool"))

    def test_definitions_publish_complete_schemas(self) -> None:
        """防止模型收到的参数契约缺少类型、必填或额外参数限制。"""
        expected = {
            "list_files": ({"path": {"type": "string"}}, []),
            "read_file": ({"path": {"type": "string"}}, ["path"]),
            "search_text": (
                {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "use_regex": {"type": "boolean"},
                },
                ["query"],
            ),
            "glob_files": (
                {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                },
                ["pattern"],
            ),
            "edit_file": (
                {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                ["path", "old_text", "new_text"],
            ),
            "create_file": (
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            "apply_patch": ({"patch": {"type": "string"}}, ["patch"]),
            "run_command": (
                {"command": {"type": "string"}, "cwd": {"type": "string"}},
                ["command"],
            ),
            "git_diff": ({}, []),
            "finish": ({"summary": {"type": "string"}}, ["summary"]),
        }

        for definition in self.registry.definitions:
            with self.subTest(tool=definition.name):
                self.assertEqual("object", definition.parameters["type"])
                self.assertEqual(expected[definition.name][0], definition.parameters["properties"])
                self.assertEqual(expected[definition.name][1], definition.parameters["required"])
                self.assertFalse(definition.parameters["additionalProperties"])

    def test_describe_returns_only_registered_static_definition(self) -> None:
        """防止 describe 暴露处理器或根据运行时输入生成不稳定说明。"""
        definition = self.registry.definitions[0]

        self.assertEqual(definition, self.registry.describe(definition.name))
        self.assertIsNone(self.registry.describe("unknown_tool"))

    def test_public_definition_schema_mutation_cannot_change_registry_validation(self) -> None:
        """防止调用方篡改嵌套 Schema 后放宽或改写内部参数校验。"""
        public_definition = self.registry.definitions[1]
        public_definition.parameters["required"].clear()
        public_definition.parameters["properties"]["path"]["type"] = "integer"

        original_definition = self.registry.definitions[1]
        self.assertEqual(["path"], original_definition.parameters["required"])
        self.assertEqual(
            "string",
            original_definition.parameters["properties"]["path"]["type"],
        )
        self.assertFalse(self.registry.execute("read_file", {}).ok)
        self.assertFalse(self.registry.execute("read_file", {"path": 1}).ok)

    def test_execute_rejects_invalid_arguments_before_handler_side_effects(self) -> None:
        """防止缺参、类型错误或额外参数在处理器和审批前继续执行。"""
        invalid_calls = (
            ("read_file", {}),
            ("read_file", {"path": 1}),
            ("run_command", {"command": "python -m unittest", "extra": True}),
        )

        for name, arguments in invalid_calls:
            with self.subTest(name=name, arguments=arguments):
                result = self.registry.execute(name, arguments)
                self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)

    def test_unknown_tool_returns_safe_result(self) -> None:
        """防止未知工具在动态分发阶段抛出异常。"""
        result = self.registry.execute("unknown_tool", {})

        self.assertFalse(result.ok)
        self.assertIn("unknown_tool", result.output)

    def test_read_and_search_return_bounded_workspace_content(self) -> None:
        """防止读取与搜索工具遗漏目标内容或擅自请求审批。"""
        read = self.registry.execute("read_file", {"path": "src/app.py"})
        search = self.registry.execute(
            "search_text",
            {"path": "src", "query": "return 41"},
        )

        self.assertTrue(read.ok)
        self.assertIn("def answer", read.output)
        self.assertTrue(search.ok)
        self.assertIn("src", search.output)
        self.assertIn("app.py:2", search.output)
        self.assertEqual([], self.approver.requests)

    def test_glob_files_matches_relative_patterns_and_hides_sensitive_paths(self) -> None:
        """glob 输出规范相对路径、目录带后缀、`**` 递归，且隐藏敏感路径。"""
        (self.workspace / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "src" / "util.py").write_text("y = 2\n", encoding="utf-8")
        (self.workspace / "src" / "sub").mkdir()
        (self.workspace / "src" / "sub" / "mod.py").write_text("m = 1\n", encoding="utf-8")
        (self.workspace / "notes.txt").write_text("z\n", encoding="utf-8")
        (self.workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")

        all_result = self.registry.execute("glob_files", {"pattern": "**/*"})
        self.assertTrue(all_result.ok, all_result.output)
        self.assertIn("src/app.py", all_result.output)
        self.assertIn("src/util.py", all_result.output)
        self.assertIn("notes.txt", all_result.output)
        self.assertNotIn(".env", all_result.output)

        py_result = self.registry.execute("glob_files", {"pattern": "src/*.py"})
        self.assertTrue(py_result.ok)
        self.assertIn("src/app.py", py_result.output)
        self.assertIn("src/util.py", py_result.output)
        self.assertNotIn("notes.txt", py_result.output)

        dir_result = self.registry.execute("glob_files", {"pattern": "src/*"})
        self.assertTrue(dir_result.ok)
        self.assertIn("src/sub/", dir_result.output.splitlines())

    def test_glob_files_rejects_escaping_or_absolute_patterns(self) -> None:
        """越界或绝对 glob 模式必须被拒绝，不能触碰工作区外路径。"""
        for pattern in ("../outside/*", "/etc/passwd", "C:\\outside\\*", "..\\x"):
            with self.subTest(pattern=pattern):
                result = self.registry.execute("glob_files", {"pattern": pattern})
                self.assertFalse(result.ok)
                self.assertNotIn("outside", result.output)

    def test_search_text_supports_regex_and_skips_ignored_binary_large_files(
        self,
    ) -> None:
        """正则搜索可用，二进制与超大文件被跳过，.gitignore 规则被尊重。"""
        (self.workspace / "README.md").write_text(
            "alpha v1.2\nbeta\n", encoding="utf-8"
        )
        (self.workspace / "ignored_dir").mkdir()
        (self.workspace / "ignored_dir" / "secret.txt").write_text(
            "should not appear\n", encoding="utf-8"
        )
        (self.workspace / ".gitignore").write_text(
            "ignored_dir/\n*.log\n", encoding="utf-8"
        )
        (self.workspace / "app.log").write_text("noise\n", encoding="utf-8")
        (self.workspace / "binary.bin").write_bytes(b"\x00\x01\x02target\x00")
        (self.workspace / "huge.txt").write_text(
            "big\n" * 50,
            encoding="utf-8",
        )
        self.registry.context.max_search_file_bytes = 200

        regex_result = self.registry.execute(
            "search_text",
            {"path": ".", "query": r"v\d+\.\d+", "use_regex": True},
        )
        self.assertTrue(regex_result.ok, regex_result.output)
        self.assertIn("README.md:1", regex_result.output)

        ignored_result = self.registry.execute(
            "search_text",
            {"path": ".", "query": "should not appear"},
        )
        self.assertTrue(ignored_result.ok)
        self.assertNotIn("ignored_dir", ignored_result.output)

        log_result = self.registry.execute(
            "search_text",
            {"path": ".", "query": "noise"},
        )
        self.assertTrue(log_result.ok)
        self.assertNotIn("app.log", log_result.output)

        binary_result = self.registry.execute(
            "search_text",
            {"path": ".", "query": "target"},
        )
        self.assertTrue(binary_result.ok)
        self.assertNotIn("binary.bin", binary_result.output)

        large_result = self.registry.execute(
            "search_text",
            {"path": ".", "query": "big"},
        )
        self.assertTrue(large_result.ok)
        self.assertNotIn("huge.txt", large_result.output)

    def test_git_diff_reports_uncommitted_changes(self) -> None:
        """git_diff 只读显示未提交变更统计；无 git 时跳过。"""
        if shutil.which("git") is None:
            self.skipTest("当前环境没有 git")
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "test"],
        ):
            subprocess.run(command, cwd=self.workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.workspace, check=True)
        (self.workspace / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

        result = self.registry.execute("git_diff", {})
        self.assertTrue(result.ok, result.output)
        self.assertIn("app.py", result.output)

        subprocess.run(["git", "checkout", "-q", "--", "src/app.py"], cwd=self.workspace, check=True)
        clean = self.registry.execute("git_diff", {})
        self.assertTrue(clean.ok)
        self.assertIn("没有未提交变更", clean.output)

    def test_git_diff_rejects_when_repo_root_outside_workspace(self) -> None:
        """git 在仓库子目录工作区执行会越界读取仓库根，必须拒绝。"""
        if shutil.which("git") is None:
            self.skipTest("当前环境没有 git")
        outer = Path(tempfile.mkdtemp(prefix="repo-outside-"))
        try:
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.com"],
                ["git", "config", "user.name", "test"],
            ):
                subprocess.run(command, cwd=outer, check=True)
            inner = outer / "sub"
            inner.mkdir()
            registry = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(inner),
                    CommandPolicy(),
                    approver=lambda _action, _detail: True,
                    timeout=5,
                )
            )
            result = registry.execute("git_diff", {})
            self.assertFalse(result.ok)
            self.assertIn("工作区", result.output)
        finally:
            shutil.rmtree(outer, ignore_errors=True)

    def test_glob_files_rejects_unbounded_patterns(self) -> None:
        """过长模式或过多 ** 会放大扫描规模，必须拒绝。"""
        for pattern in ("x" * 300, "**/**/**/*.py"):
            with self.subTest(pattern=pattern):
                result = self.registry.execute("glob_files", {"pattern": pattern})
                self.assertFalse(result.ok)

    def test_search_text_limits_regex_and_ignores_build_directories(self) -> None:
        """正则长度受限防灾难性回溯；`build/` 规则忽略任意层级同名目录。"""
        overlong = self.registry.execute(
            "search_text",
            {"path": ".", "query": "a" * 300, "use_regex": True},
        )
        self.assertFalse(overlong.ok)
        self.assertIn("过长", overlong.output)

        (self.workspace / "build").mkdir()
        (self.workspace / "src" / "build").mkdir()
        (self.workspace / "build" / "root.txt").write_text("trace\n", encoding="utf-8")
        (self.workspace / "src" / "build" / "nested.txt").write_text(
            "trace\n", encoding="utf-8"
        )
        (self.workspace / ".gitignore").write_text("build/\n", encoding="utf-8")

        result = self.registry.execute(
            "search_text",
            {"path": ".", "query": "trace"},
        )
        self.assertTrue(result.ok)
        self.assertNotIn("build/", result.output)

    def test_edit_requires_approval_and_writes_exact_replacement(self) -> None:
        """防止模型在用户未确认时写文件，并捕获替换错位。"""
        result = self.registry.execute(
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": "return 41",
                "new_text": "return 42",
            },
        )

        self.assertTrue(result.ok, result.output)
        self.assertEqual(
            "def answer():\n    return 42\n",
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
        )
        self.assertEqual("edit_file", self.approver.requests[0][0])
        self.assertIn("-    return 41", self.approver.requests[0][1])
        self.assertIn("+    return 42", self.approver.requests[0][1])
        self.assertEqual([], list((self.workspace / "src").glob("*.tmp")))

    def test_edit_file_records_before_and_committed_after_snapshots(self) -> None:
        """防止编辑账本遗漏原始内容，或记录尚未提交的预测后态。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        registry = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(self.workspace),
                command_policy=CommandPolicy(),
                approver=RecordingApprover([True]),
                change_journal=journal,
            )
        )

        result = registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
        )
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertTrue(result.ok)
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual("def answer():\n    return 41\n", change_set.changes[0].before.content)
        self.assertEqual("def answer():\n    return 42\n", change_set.changes[0].after.content)
        metadata = (self.workspace / "src" / "app.py").stat()
        self.assertEqual(
            FileIdentity(metadata.st_dev, metadata.st_ino),
            change_set.changes[0].after.identity,
        )

    def test_sequential_write_rejects_external_state_and_taints_without_absorbing_source(
        self,
    ) -> None:
        """防止连续工具调用把两次调用之间的外部源码吸收到任务后态。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        self.approver.decisions.append(True)
        target = self.workspace / "src" / "app.py"

        first = self.registry.execute(
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": "return 41",
                "new_text": "return 42",
            },
        )
        external_sentinel = "EXTERNAL-PRIVATE-SOURCE-SENTINEL-73A1"
        replacement = self.workspace / "src" / "external-replacement.py"
        replacement.write_text(f"{external_sentinel}\n", encoding="utf-8")
        os.replace(replacement, target)

        second = self.registry.execute(
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": external_sentinel,
                "new_text": "agent-second-write",
            },
        )
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual("任务内文件状态不连续：src/app.py", second.output)
        self.assertEqual(1, len(self.approver.requests))
        self.assertEqual(f"{external_sentinel}\n", target.read_text(encoding="utf-8"))
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/app.py",), change_set.tainted_paths)
        self.assertEqual(
            "def answer():\n    return 42\n",
            change_set.changes[0].after.content,
        )
        self.assertNotIn(external_sentinel, repr(change_set))
        with self.assertRaisesRegex(ChangeJournalError, "src/app.py"):
            render_change_set_diff(change_set)
        with self.assertRaisesRegex(tools_module.PolicyError, "src/app.py"):
            self.registry.preview_undo(change_set)
        execution = self.registry.undo_change_set(change_set)
        self.assertFalse(execution.ok)
        self.assertEqual(("src/app.py",), execution.conflicts)
        self.assertEqual(f"{external_sentinel}\n", target.read_text(encoding="utf-8"))

    def test_net_zero_writes_keep_continuity_guard_against_later_external_state(
        self,
    ) -> None:
        """防止 A→B→A 的净零聚合遗失最后一次已证明后态。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        self.approver.decisions.extend((True, True))
        target = self.workspace / "src" / "app.py"

        first = self.registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
        )
        second = self.registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "return 42", "new_text": "return 41"},
        )
        external_sentinel = "NET-ZERO-EXTERNAL-SENTINEL-1A4E"
        replacement = self.workspace / "src" / "net-zero-external.py"
        replacement.write_text(f"{external_sentinel}\n", encoding="utf-8")
        os.replace(replacement, target)

        third = self.registry.execute(
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": external_sentinel,
                "new_text": "agent-third-write",
            },
        )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(third.ok)
        self.assertEqual("任务内文件状态不连续：src/app.py", third.output)
        self.assertEqual(2, len(self.approver.requests))
        self.assertEqual(f"{external_sentinel}\n", target.read_text(encoding="utf-8"))
        self.assertTrue(journal.is_tainted("src/app.py"))
        self.assertIsNone(journal.seal_task((), "not-run"))

    def test_edit_file_rejects_post_publish_external_snapshot_without_absorbing_it(
        self,
    ) -> None:
        """防止单文件替换发布后的竞态外部源码被记为任务 after。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        self.approver.decisions.append(True)
        target = self.workspace / "src" / "app.py"
        first = self.registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
        )
        external_sentinel = "EDIT-POST-PUBLISH-EXTERNAL-SENTINEL-881F\n"

        with patch_late_commit_race(
            stage="after",
            target="app.py",
            content=external_sentinel,
        ):
            second = self.registry.execute(
                "edit_file",
                {"path": "src/app.py", "old_text": "return 42", "new_text": "return 43"},
            )
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(external_sentinel, target.read_text(encoding="utf-8"))
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/app.py",), change_set.tainted_paths)
        self.assertEqual("def answer():\n    return 42\n", change_set.changes[0].after.content)
        self.assertNotIn(external_sentinel.strip(), repr(change_set))
        self.assertNotIn(external_sentinel.strip(), second.output)

    def test_create_file_rejects_post_publish_external_snapshot_without_absorbing_it(
        self,
    ) -> None:
        """防止单文件创建发布后的竞态外部源码被记入任务账本。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        self.approver.decisions.append(True)
        first = self.registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
        )
        external_sentinel = "CREATE-POST-PUBLISH-EXTERNAL-SENTINEL-291C\n"

        with patch_late_commit_race(
            stage="after",
            target="created.py",
            content=external_sentinel,
        ):
            second = self.registry.execute(
                "create_file",
                {"path": "src/created.py", "content": "agent-created\n"},
            )
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(
            external_sentinel,
            (self.workspace / "src" / "created.py").read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/created.py",), change_set.tainted_paths)
        self.assertEqual(("src/app.py",), tuple(change.path for change in change_set.changes))
        self.assertNotIn(external_sentinel.strip(), repr(change_set))
        self.assertNotIn(external_sentinel.strip(), second.output)

    def test_edit_file_rejects_journal_over_budget_before_approval(self) -> None:
        """防止预算不足时仍请求审批或触碰原文件。"""
        journal = ChangeJournal(max_chars=10)
        journal.begin_task((), "not-run")
        approver = RecordingApprover([])
        registry = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(self.workspace),
                command_policy=CommandPolicy(),
                approver=approver,
                change_journal=journal,
            )
        )
        target = self.workspace / "src" / "app.py"
        original = target.read_bytes()

        result = registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "return 41", "new_text": "return 42"},
        )

        self.assertFalse(result.ok)
        self.assertIn("变更预算不足", result.output)
        self.assertEqual([], approver.requests)
        self.assertEqual(original, target.read_bytes())
        self.assertIsNone(journal.seal_task((), "not-run"))

    def test_successful_writes_return_policy_canonical_relative_path(self) -> None:
        """防止工具成功后仍把绝对或 dotdot 输入暴露给会话元数据。"""
        target = self.workspace / "src" / "app.py"
        absolute_edit = self.registry.execute(
            "edit_file",
            {
                "path": str(target),
                "old_text": "return 41",
                "new_text": "return 42",
            },
        )
        self.approver.decisions.append(True)
        dotdot_edit = self.registry.execute(
            "edit_file",
            {
                "path": "src/../src/app.py",
                "old_text": "return 42",
                "new_text": "return 43",
            },
        )

        self.assertTrue(absolute_edit.ok)
        self.assertTrue(dotdot_edit.ok)
        self.assertEqual("src/app.py", getattr(absolute_edit, "relative_path", None))
        self.assertEqual("src/app.py", getattr(dotdot_edit, "relative_path", None))

    def test_edit_approval_receives_complete_diff_beyond_output_limit(self) -> None:
        """防止审批详情复用模型输出上限，导致关键尾部修改被静默截断。"""
        old_text = "A" * 20_100 + "\nold-tail-marker\n"
        new_text = "B" * 20_100 + "\ncritical-approved-tail-marker\n"
        target = self.workspace / "src" / "large.txt"
        target.write_text(old_text, encoding="utf-8")

        result = self.registry.execute(
            "edit_file",
            {
                "path": "src/large.txt",
                "old_text": old_text,
                "new_text": new_text,
            },
        )

        self.assertTrue(result.ok, result.output)
        detail = self.approver.requests[0][1]
        self.assertGreater(len(detail), self.registry.context.max_output_chars)
        self.assertIn("critical-approved-tail-marker", detail)
        self.assertNotIn("已截断", detail)

    def test_rejected_edit_leaves_file_unchanged(self) -> None:
        """防止审批拒绝后仍产生部分写入。"""
        self.approver.decisions = [False]
        original = (self.workspace / "src" / "app.py").read_bytes()

        result = self.registry.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "41", "new_text": "42"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(original, (self.workspace / "src" / "app.py").read_bytes())

    def test_edit_conflict_is_rejected_before_approval(self) -> None:
        """防止旧文本缺失或重复时修改错误位置。"""
        for old_text in ("missing", "return"):
            if old_text == "return":
                (self.workspace / "src" / "app.py").write_text(
                    "return 1\nreturn 2\n",
                    encoding="utf-8",
                )
            with self.subTest(old_text=old_text):
                result = self.registry.execute(
                    "edit_file",
                    {"path": "src/app.py", "old_text": old_text, "new_text": "changed"},
                )
                self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)

    def test_create_file_requires_approval_and_writes_new_file(self) -> None:
        """防止未获批准时创建文件，并确保批准后以 UTF-8 写入新内容。"""
        result = self.registry.execute(
            "create_file",
            {"path": "src/new.py", "content": "答案 = '安全'\n"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            "答案 = '安全'\n",
            (self.workspace / "src" / "new.py").read_text(encoding="utf-8"),
        )
        self.assertEqual("create_file", self.approver.requests[0][0])
        self.assertIn("+答案 = '安全'", self.approver.requests[0][1])
        self.assertEqual([], list((self.workspace / "src").glob("*.tmp")))

    def test_apply_patch_modifies_and_creates_files_with_one_approval(self) -> None:
        """防止多文件补丁分批审批、乱序报告或只提交部分成功目标。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- /dev/null\n"
            "+++ b/src/new.py\n"
            "@@ -0,0 +1 @@\n"
            "+created = True\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertTrue(result.ok, result.output)
        self.assertEqual(("src/app.py", "src/new.py"), result.modified_paths)
        self.assertEqual(1, len(self.approver.requests))
        self.assertEqual("apply_patch", self.approver.requests[0][0])
        self.assertIn("--- src/app.py", self.approver.requests[0][1])
        self.assertEqual(
            "def answer():\n    return 42\n",
            (self.workspace / "src" / "app.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "created = True\n",
            (self.workspace / "src" / "new.py").read_text(encoding="utf-8"),
        )

    def test_apply_patch_empty_creation_names_target_in_approval(self) -> None:
        """防止合法空文件创建以空白审批详情绕过用户确认。"""
        patch_text = (
            "--- /dev/null\n"
            "+++ b/src/empty.py\n"
            "@@ -0,0 +0,0 @@\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertTrue(result.ok, result.output)
        self.assertEqual(("src/empty.py",), result.modified_paths)
        self.assertEqual(1, len(self.approver.requests))
        approval_detail = self.approver.requests[0][1]
        self.assertIn("--- /dev/null", approval_detail)
        self.assertIn("+++ src/empty.py", approval_detail)
        self.assertIn("创建空文件", approval_detail)
        self.assertEqual("", (self.workspace / "src" / "empty.py").read_text(encoding="utf-8"))

    def test_apply_patch_approval_marks_missing_newlines_and_separates_files(self) -> None:
        """防止审批 diff 的无换行源码与下一文件头粘连。"""
        first = self.workspace / "src" / "app.py"
        second = self.workspace / "src" / "other.py"
        first.write_text("old", encoding="utf-8")
        second.write_text("before", encoding="utf-8")
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "\\ No newline at end of file\n"
            "+after\n"
            "\\ No newline at end of file\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertTrue(result.ok, result.output)
        approval = self.approver.requests[0][1]
        self.assertIn(
            "-old\n\\ No newline at end of file\n"
            "+new\n\\ No newline at end of file\n",
            approval,
        )
        self.assertIn("\n--- src/other.py\n+++ src/other.py\n", approval)

    def test_apply_patch_rejects_existing_file_no_op_before_approval(self) -> None:
        """防止无净变化补丁仍替换文件并把路径报告为已修改。"""
        target = self.workspace / "src" / "app.py"
        before = target.stat()
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "     return 41\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})
        after = target.stat()

        self.assertFalse(result.ok)
        self.assertEqual((), result.modified_paths)
        self.assertEqual([], self.approver.requests)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(
            "def answer():\n    return 41\n",
            target.read_text(encoding="utf-8"),
        )

    def test_apply_patch_hides_approver_exception_detail(self) -> None:
        """防止审批器异常把完整 diff 或源码原文复制到工具结果。"""
        sentinel = "APPROVER-PATCH-PRIVATE-SENTINEL-71C4"
        target = self.workspace / "src" / "app.py"
        original = target.read_text(encoding="utf-8")
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            f"+    return 42  # {sentinel}\n"
        )

        for exception_type in (OSError, ValueError, TypeError):
            with self.subTest(exception_type=exception_type.__name__):
                def fail_approval(action: str, detail: str) -> bool:
                    self.assertEqual("apply_patch", action)
                    self.assertIn(sentinel, detail)
                    raise exception_type(f"{sentinel}\n{detail}")

                self.registry.context.approver = fail_approval
                result = self.registry.execute("apply_patch", {"patch": patch_text})

                self.assertFalse(result.ok)
                self.assertIn("补丁审批失败", result.output)
                self.assertNotIn(sentinel, result.output)
                self.assertNotIn("return 42", result.output)
                self.assertEqual(original, target.read_text(encoding="utf-8"))

    def test_apply_patch_maps_preflight_filesystem_errors_to_safe_paths(self) -> None:
        """防止预检边界异常泄漏绝对路径、临时名或自由文本。"""
        absolute_sentinel = str(self.workspace / "PRIVATE-ABSOLUTE-SENTINEL")
        temporary_sentinel = ".app.py.PRIVATE-TEMP-SENTINEL.tmp"
        unicode_sentinel = "PRIVATE-UNICODE-SENTINEL-雪"
        value_sentinel = "PRIVATE-VALUE-SENTINEL"
        type_sentinel = "PRIVATE-TYPE-SENTINEL"
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
        )
        failures = (
            patch.object(
                tools_module._DirectoryBinding,
                "open",
                side_effect=OSError(f"{absolute_sentinel} {temporary_sentinel}"),
            ),
            patch.object(
                ToolHandler,
                "_snapshot",
                side_effect=UnicodeError(unicode_sentinel),
            ),
            patch.object(
                tools_module._DirectoryBinding,
                "open",
                side_effect=ValueError(f"{absolute_sentinel} {value_sentinel}"),
            ),
            patch.object(
                tools_module._DirectoryBinding,
                "open",
                side_effect=TypeError(f"{temporary_sentinel} {type_sentinel}"),
            ),
        )

        for failure in failures:
            with self.subTest(failure=failure):
                with failure:
                    result = self.registry.execute("apply_patch", {"patch": patch_text})

                self.assertFalse(result.ok)
                self.assertEqual("补丁文件系统操作失败：src/app.py", result.output)
                self.assertNotIn(absolute_sentinel, result.output)
                self.assertNotIn(temporary_sentinel, result.output)
                self.assertNotIn(unicode_sentinel, result.output)
                self.assertNotIn(value_sentinel, result.output)
                self.assertNotIn(type_sentinel, result.output)
                self.assertEqual(("src/app.py",), result.audit_paths)
                self.assertEqual(
                    len("    return 41\n") + len("    return 42\n"),
                    result.change_chars,
                )
        self.assertEqual([], self.approver.requests)

    def test_apply_patch_context_failure_keeps_safe_audit_metadata(self) -> None:
        """防止纯解析成功后的 hunk 失败把路径与变更字符数审计清空。"""
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 99\n"
            "+    return 42\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertEqual(("src/app.py",), result.audit_paths)
        self.assertEqual(
            len("    return 99\n") + len("    return 42\n"),
            result.change_chars,
        )
        self.assertEqual([], self.approver.requests)

    def test_apply_patch_rejects_malformed_second_file_before_approval(self) -> None:
        """防止解析首个文件后就在第二个畸形文件暴露前产生审批或写入。"""
        target = self.workspace / "src" / "app.py"
        original = target.read_bytes()
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- /dev/null\n"
            "@@ -0,0 +1 @@\n"
            "+malformed = True\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)
        self.assertEqual(original, target.read_bytes())

    def test_apply_patch_rejects_sensitive_path_before_approval(self) -> None:
        """防止多文件补丁绕过工作区敏感路径策略。"""
        patch_text = (
            "--- /dev/null\n"
            "+++ b/.local/unsafe.py\n"
            "@@ -0,0 +1 @@\n"
            "+unsafe = True\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)

    def test_apply_patch_rejects_parent_traversal_before_approval(self) -> None:
        """防止补丁头中的父目录穿越在路径规范化前进入审批。"""
        patch_text = (
            "--- /dev/null\n"
            "+++ b/src/../outside.py\n"
            "@@ -0,0 +1 @@\n"
            "+outside = True\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)

    def test_apply_patch_rejects_existing_create_target_before_approval(self) -> None:
        """防止创建补丁覆盖已有文件或把冲突推迟到审批之后。"""
        target = self.workspace / "src" / "app.py"
        original = target.read_bytes()
        patch_text = (
            "--- /dev/null\n"
            "+++ b/src/app.py\n"
            "@@ -0,0 +1 @@\n"
            "+overwritten = True\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)
        self.assertEqual(original, target.read_bytes())

    def test_apply_patch_rejects_budget_overflow_before_approval(self) -> None:
        """防止多文件预测变更超预算后仍请求审批。"""
        journal = ChangeJournal(max_chars=10)
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        target = self.workspace / "src" / "app.py"
        original = target.read_bytes()
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertIn("变更预算不足", result.output)
        self.assertEqual([], self.approver.requests)
        self.assertEqual(original, target.read_bytes())
        self.assertIsNone(journal.seal_task((), "not-run"))

    def test_apply_patch_rechecks_all_targets_after_approval_before_writing(self) -> None:
        """防止审批期间一个目标变化后仍先写入另一个目标。"""
        first = self.workspace / "src" / "app.py"
        second = self.workspace / "src" / "other.py"
        second.write_text("value = 1\n", encoding="utf-8")
        first_original = first.read_bytes()

        def mutate_second(action: str, detail: str) -> bool:
            self.assertEqual("apply_patch", action)
            self.assertIn("src/other.py", detail)
            second.write_text("external = True\n", encoding="utf-8")
            return True

        self.registry.context.approver = mutate_second
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )

        result = self.registry.execute("apply_patch", {"patch": patch_text})

        self.assertFalse(result.ok)
        self.assertEqual(first_original, first.read_bytes())
        self.assertEqual("external = True\n", second.read_text(encoding="utf-8"))

    def test_apply_patch_rechecks_each_target_immediately_before_publish(self) -> None:
        """防止全量复核后、逐文件发布前的晚到外部替换被覆盖。"""
        first = self.workspace / "src" / "app.py"
        second = self.workspace / "src" / "other.py"
        second.write_text("value = 1\n", encoding="utf-8")
        first_original = first.read_text(encoding="utf-8")
        external_sentinel = "LATE-RACE-EXTERNAL-BEFORE-PUBLISH-42D9\n"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )

        with patch_late_commit_race(
            stage="before",
            target="other.py",
            content=external_sentinel,
        ):
            result = self.registry.execute("apply_patch", {"patch": patch_text})
        change_set = journal.seal_task((), "not-run")

        self.assertFalse(result.ok)
        self.assertEqual(first_original, first.read_text(encoding="utf-8"))
        self.assertEqual(external_sentinel, second.read_text(encoding="utf-8"))
        self.assertIsNone(change_set)
        self.assertNotIn(external_sentinel.strip(), result.output)
        self.assertNotIn(str(self.workspace), result.output)

    def test_apply_patch_rejects_post_publish_snapshot_mismatch_as_tainted(self) -> None:
        """防止发布后的外部替换被记录成工具 expected_after 或可撤销状态。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        self.approver.decisions.append(True)
        target = self.workspace / "src" / "app.py"
        first = self.registry.execute(
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": "return 41",
                "new_text": "return 42",
            },
        )
        external_sentinel = "LATE-RACE-EXTERNAL-AFTER-PUBLISH-87B1\n"
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 42\n"
            "+    return 43\n"
        )

        with patch_late_commit_race(
            stage="after",
            target="app.py",
            content=external_sentinel,
        ):
            second = self.registry.execute("apply_patch", {"patch": patch_text})
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(external_sentinel, target.read_text(encoding="utf-8"))
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/app.py",), change_set.tainted_paths)
        self.assertEqual(
            "def answer():\n    return 42\n",
            change_set.changes[0].after.content,
        )
        self.assertNotIn(external_sentinel.strip(), repr(change_set))
        self.assertNotIn(external_sentinel.strip(), second.output)

    def test_apply_patch_read_only_rejects_before_parsing_or_approval(self) -> None:
        """防止只读模式解析自由补丁文本或请求审批。"""
        read_only = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                self.approver,
                read_only=True,
            )
        )

        for arguments in (
            {"patch": "not-a-patch"},
            {"patch": "not-a-patch", "extra": True},
        ):
            with self.subTest(arguments=arguments):
                with patch.object(
                    write_module,
                    "parse_unified_diff",
                    side_effect=AssertionError("read-only must not parse patch text"),
                ):
                    result = read_only.execute("apply_patch", arguments)

                self.assertFalse(result.ok)
                if "extra" not in arguments:
                    self.assertIn("只读", result.output)
        self.assertEqual([], self.approver.requests)

    def test_apply_patch_rejects_invalid_provider_arguments(self) -> None:
        """防止缺参、错误类型或额外字段进入补丁解析和审批。"""
        valid_patch = (
            "--- /dev/null\n"
            "+++ b/src/extra-forbidden.py\n"
            "@@ -0,0 +1 @@\n"
            "+created = True\n"
        )
        invalid_arguments = (
            {},
            {"patch": 42},
            {"patch": valid_patch, "extra": True},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with patch.object(
                    write_module,
                    "parse_unified_diff",
                    side_effect=AssertionError("invalid arguments must not parse patch text"),
                ):
                    result = self.registry.execute("apply_patch", arguments)
                self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "src" / "extra-forbidden.py").exists())
        self.assertEqual([], self.approver.requests)

    def test_apply_patch_compensates_prior_commit_when_later_publish_fails(self) -> None:
        """防止第二个目标发布失败后保留第一个目标的部分提交。"""
        first = self.workspace / "src" / "app.py"
        second = self.workspace / "src" / "other.py"
        second.write_text("value = 1\n", encoding="utf-8")
        first_original = first.read_text(encoding="utf-8")
        second_original = second.read_text(encoding="utf-8")
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )

        with patch_binding_publish_failure():
            result = self.registry.execute("apply_patch", {"patch": patch_text})
        change_set = journal.seal_task((), "not-run")

        self.assertFalse(result.ok)
        self.assertEqual(first_original, first.read_text(encoding="utf-8"))
        self.assertEqual(second_original, second.read_text(encoding="utf-8"))
        self.assertIsNone(change_set)

    def test_apply_patch_preserves_actual_journal_state_when_compensation_fails(
        self,
    ) -> None:
        """防止补偿失败后把真实残留修改从结果和账本中抹掉。"""
        first = self.workspace / "src" / "app.py"
        second = self.workspace / "src" / "other.py"
        second.write_text("value = 1\n", encoding="utf-8")
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )

        with patch_binding_publish_failure(rollback_fails=True):
            result = self.registry.execute("apply_patch", {"patch": patch_text})
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertFalse(result.ok)
        self.assertIn("src/app.py", result.output)
        self.assertIn("src/other.py", result.output)
        self.assertNotIn("PUBLISH-SECOND-SENTINEL", result.output)
        self.assertNotIn("ROLLBACK-FIRST-SENTINEL", result.output)
        self.assertNotIn(str(self.workspace), result.output)
        self.assertNotIn("return 42", result.output)
        self.assertEqual("def answer():\n    return 42\n", first.read_text(encoding="utf-8"))
        self.assertEqual("value = 1\n", second.read_text(encoding="utf-8"))
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/app.py",), tuple(change.path for change in change_set.changes))
        self.assertEqual(
            "def answer():\n    return 42\n",
            change_set.changes[0].after.content,
        )

    def test_apply_patch_removes_committed_new_file_after_later_publish_failure(
        self,
    ) -> None:
        """防止后续发布失败时遗漏经身份核验的新建目标补偿。"""
        second = self.workspace / "src" / "other.py"
        second.write_text("value = 1\n", encoding="utf-8")
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        state: dict[str, object] = {}
        patch_text = (
            "--- /dev/null\n"
            "+++ b/src/aaa-new.py\n"
            "@@ -0,0 +1 @@\n"
            "+created = True\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )

        with patch_new_file_compensation_probe(state):
            result = self.registry.execute("apply_patch", {"patch": patch_text})
        change_set = journal.seal_task((), "not-run")

        self.assertFalse(result.ok)
        self.assertEqual((), result.modified_paths)
        self.assertFalse((self.workspace / "src" / "aaa-new.py").exists())
        self.assertEqual("value = 1\n", second.read_text(encoding="utf-8"))
        self.assertEqual(["aaa-new.py"], state.get("verified_deletes"))
        self.assertIsNone(change_set)

    def test_apply_patch_refuses_to_overwrite_external_replacement_during_compensation(
        self,
    ) -> None:
        """防止补偿覆盖发布失败前由外部替换的真实目标状态。"""
        first = self.workspace / "src" / "app.py"
        second = self.workspace / "src" / "other.py"
        second.write_text("value = 1\n", encoding="utf-8")
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        state: dict[str, object] = {}
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def answer():\n"
            "-    return 41\n"
            "+    return 42\n"
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )

        with patch_external_replacement_before_compensation(state):
            result = self.registry.execute("apply_patch", {"patch": patch_text})
        change_set = journal.seal_task(("src/app.py",), "not-run")

        self.assertFalse(result.ok)
        self.assertEqual(("src/app.py",), result.modified_paths)
        self.assertTrue(state.get("external_replaced"))
        self.assertNotIn("unsafe_overwrite", state)
        self.assertEqual("external = True\n", first.read_text(encoding="utf-8"))
        self.assertEqual("value = 1\n", second.read_text(encoding="utf-8"))
        self.assertIsNotNone(change_set)
        assert change_set is not None
        change = change_set.changes[0]
        self.assertEqual("src/app.py", change.path)
        self.assertEqual("def answer():\n    return 42\n", change.after.content)
        self.assertEqual(
            ("src/app.py",),
            change_set.tainted_paths,
        )
        self.assertNotIn("external = True", repr(change_set))
        with self.assertRaisesRegex(tools_module.PolicyError, "src/app.py"):
            self.registry.preview_undo(change_set)
        execution = self.registry.undo_change_set(change_set)
        self.assertFalse(execution.ok)
        self.assertEqual(("src/app.py",), execution.conflicts)
        self.assertEqual("external = True\n", first.read_text(encoding="utf-8"))

    def test_create_file_records_missing_before_and_published_after_snapshot(self) -> None:
        """防止新建账本伪造前态，或沿用临时文件而非发布目标的身份。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        registry = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(self.workspace),
                command_policy=CommandPolicy(),
                approver=RecordingApprover([True]),
                change_journal=journal,
            )
        )

        result = registry.execute(
            "create_file",
            {"path": "src/new.py", "content": "answer = 42\n"},
        )
        change_set = journal.seal_task(("src/new.py",), "not-run")

        self.assertTrue(result.ok)
        self.assertIsNotNone(change_set)
        assert change_set is not None
        change = change_set.changes[0]
        self.assertIsNone(change.before)
        self.assertEqual("answer = 42\n", change.after.content)
        metadata = (self.workspace / "src" / "new.py").stat()
        self.assertEqual(FileIdentity(metadata.st_dev, metadata.st_ino), change.after.identity)

    def test_create_approval_receives_complete_diff_beyond_output_limit(self) -> None:
        """防止超长新文件的审批详情遗漏末尾关键内容。"""
        content = "x" * 20_100 + "\ncritical-create-tail-marker\n"

        result = self.registry.execute(
            "create_file",
            {"path": "src/large-created.txt", "content": content},
        )

        self.assertTrue(result.ok)
        detail = self.approver.requests[0][1]
        self.assertGreater(len(detail), self.registry.context.max_output_chars)
        self.assertIn("critical-create-tail-marker", detail)
        self.assertNotIn("已截断", detail)

    def test_empty_create_approval_explicitly_identifies_empty_file(self) -> None:
        """防止空文件创建产生空白审批详情，使用户无法判断将发生什么。"""
        result = self.registry.execute(
            "create_file",
            {"path": "src/empty.txt", "content": ""},
        )

        self.assertTrue(result.ok)
        self.assertIn("空文件", self.approver.requests[0][1])
        self.assertEqual("", (self.workspace / "src" / "empty.txt").read_text(encoding="utf-8"))

    def test_rejected_create_file_does_not_create_target(self) -> None:
        """防止用户拒绝创建时留下目标文件或临时文件。"""
        self.approver.decisions = [False]

        result = self.registry.execute(
            "create_file",
            {"path": "src/rejected.py", "content": "value = 1\n"},
        )

        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "src" / "rejected.py").exists())
        self.assertEqual([], list((self.workspace / "src").glob("*.tmp")))

    def test_create_file_rejects_existing_target_without_overwriting(self) -> None:
        """防止创建工具覆盖已有文件。"""
        target = self.workspace / "src" / "existing.py"
        target.write_text("original = True\n", encoding="utf-8")

        result = self.registry.execute(
            "create_file",
            {"path": "src/existing.py", "content": "original = False\n"},
        )

        self.assertFalse(result.ok)
        self.assertEqual("original = True\n", target.read_text(encoding="utf-8"))
        self.assertEqual([], self.approver.requests)

    def test_create_file_rejects_target_created_during_approval(self) -> None:
        """防止审批期间由外部创建的同名文件被原子替换覆盖。"""
        target = self.workspace / "src" / "racing.py"

        def approve_after_creating_target(action: str, _detail: str) -> bool:
            self.assertEqual("create_file", action)
            target.write_text("external = True\n", encoding="utf-8")
            return True

        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approve_after_creating_target,
            )
        )
        result = self.registry.execute(
            "create_file",
            {"path": "src/racing.py", "content": "agent = True\n"},
        )

        self.assertFalse(result.ok)
        self.assertEqual("external = True\n", target.read_text(encoding="utf-8"))
        self.assertEqual([], list((self.workspace / "src").glob("*.tmp")))

    def test_create_file_rejects_parent_identity_change_after_approval(self) -> None:
        """跨平台执行父目录身份变化分支，防止仅靠路径文本判断同一目录。"""
        original_parent = self.workspace / "src"
        alternate_parent = self.workspace / "alternate"
        alternate_parent.mkdir()

        class RebindingPolicy(WorkspacePolicy):
            switched = False

            def resolve_path(
                self,
                path: str | Path,
                *,
                must_exist: bool = True,
            ) -> Path:
                if self.switched and str(path) == "src/rebound.py":
                    return alternate_parent / "rebound.py"
                return super().resolve_path(path, must_exist=must_exist)

        policy = RebindingPolicy(self.workspace)

        def approve_after_rebinding(_action: str, _detail: str) -> bool:
            policy.switched = True
            return True

        registry = ToolRegistry(
            ToolContext(policy, CommandPolicy(), approve_after_rebinding)
        )

        result = registry.execute(
            "create_file",
            {"path": "src/rebound.py", "content": "value = 1\n"},
        )

        self.assertFalse(result.ok)
        self.assertFalse((original_parent / "rebound.py").exists())
        self.assertFalse((alternate_parent / "rebound.py").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_create_file_prevents_or_rejects_parent_symlink_swap(self) -> None:
        """审批期父目录若被外链替换，必须阻止攻击或安全失败且外部零写入。"""
        source_parent = self.workspace / "src"
        saved_parent = self.workspace / "src-saved"
        outside_parent = self.workspace.parent / f"{self.workspace.name}-outside"
        outside_parent.mkdir(exist_ok=True)
        attack_blocked = False

        def approve_during_attack(_action: str, _detail: str) -> bool:
            nonlocal attack_blocked
            try:
                source_parent.rename(saved_parent)
                source_parent.symlink_to(outside_parent, target_is_directory=True)
            except OSError:
                attack_blocked = True
            return True

        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approve_during_attack,
            )
        )
        try:
            result = registry.execute(
                "create_file",
                {"path": "src/escaped.py", "content": "outside = False\n"},
            )

            self.assertFalse((outside_parent / "escaped.py").exists())
            if attack_blocked and source_parent.exists():
                self.assertTrue(result.ok)
                self.assertTrue((source_parent / "escaped.py").exists())
            else:
                self.assertFalse(result.ok)
                self.assertFalse((saved_parent / "escaped.py").exists())
        finally:
            (outside_parent / "escaped.py").unlink(missing_ok=True)
            if source_parent.is_symlink():
                source_parent.unlink()
            if saved_parent.exists() and not source_parent.exists():
                saved_parent.rename(source_parent)
            outside_parent.rmdir()

    def test_create_file_rejects_missing_parent_before_approval(self) -> None:
        """防止父目录不存在时请求审批或留下文件残留。"""
        target = self.workspace / "missing" / "new.py"

        result = self.registry.execute(
            "create_file",
            {"path": "missing/new.py", "content": "value = 1\n"},
        )

        self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())

    def test_create_file_fails_safely_when_atomic_publish_is_unavailable(self) -> None:
        """防止不可覆盖发布不可用时降级为可见空文件或覆盖写入。"""
        target = self.workspace / "src" / "no-link.py"

        with patch("tricoder.tools.binding.os.link", side_effect=OSError("hard links unavailable")):
            result = self.registry.execute(
                "create_file",
                {"path": "src/no-link.py", "content": "complete = True\n"},
            )

        self.assertFalse(result.ok)
        self.assertFalse(target.exists())
        self.assertEqual([], list((self.workspace / "src").glob("*.tmp")))

    def test_write_fails_before_approval_when_posix_dir_fd_primitives_are_missing(
        self,
    ) -> None:
        """POSIX 缺少安全发布所需能力时不得先审批再晚失败。"""
        limited_support = {os.open, os.link, os.unlink, os.stat}

        with (
            patch("tricoder.tools.binding._is_windows", return_value=False, create=True),
            patch("tricoder.tools.binding.os.supports_dir_fd", limited_support),
        ):
            result = self.registry.execute(
                "create_file",
                {"path": "src/unsupported.py", "content": "value = 1\n"},
            )

        self.assertFalse(result.ok)
        self.assertIn("安全目录绑定", result.output)
        self.assertEqual([], self.approver.requests)
        self.assertFalse((self.workspace / "src" / "unsupported.py").exists())

    def test_create_file_reports_success_with_warning_after_cleanup_failure(self) -> None:
        """硬链接发布是提交点；之后清理失败不得把已创建文件伪装成失败。"""
        target = self.workspace / "src" / "committed.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal

        with patch_binding_cleanup_failure(target):
            result = self.registry.execute(
                "create_file",
                {"path": "src/committed.py", "content": "committed = True\n"},
            )

        self.assertTrue(result.ok)
        self.assertIn("清理警告", result.output)
        self.assertEqual("committed = True\n", target.read_text(encoding="utf-8"))
        change_set = journal.seal_task(("src/committed.py",), "not-run")
        self.assertEqual("src/committed.py", change_set.changes[0].path)

    def test_edit_reports_success_with_warning_after_binding_close_failure(self) -> None:
        """原子替换成功后关闭目录绑定失败，不得把真实修改伪装成失败。"""
        target = self.workspace / "src" / "app.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal

        with patch_binding_close_failure():
            result = self.registry.execute(
                "edit_file",
                {
                    "path": "src/app.py",
                    "old_text": "return 41",
                    "new_text": "return 42",
                },
            )

        self.assertTrue(result.ok)
        self.assertIn("关闭警告", result.output)
        self.assertEqual(
            "def answer():\n    return 42\n",
            target.read_text(encoding="utf-8"),
        )
        change_set = journal.seal_task(("src/app.py",), "not-run")
        self.assertEqual("src/app.py", change_set.changes[0].path)

    def test_edit_taints_expected_snapshot_when_post_commit_read_fails(
        self,
    ) -> None:
        """原子替换后无法核验公开目标时只能记录预期态并标记 taint。"""
        target = self.workspace / "src" / "app.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        real_snapshot = ToolHandler._snapshot

        def fail_published_target_read(
            binding: object,
            name: str,
            relative: str,
        ) -> object:
            if name == "app.py" and "return 42" in target.read_text(encoding="utf-8"):
                raise OSError("simulated post-commit snapshot failure")
            return real_snapshot(binding, name, relative)  # type: ignore[arg-type]

        with patch.object(
                ToolHandler,
                "_snapshot",
            side_effect=fail_published_target_read,
        ):
            result = self.registry.execute(
                "edit_file",
                {
                    "path": "src/app.py",
                    "old_text": "return 41",
                    "new_text": "return 42",
                },
            )

        self.assertFalse(result.ok)
        self.assertEqual("文件发布后状态无法验证：src/app.py", result.output)
        self.assertEqual("def answer():\n    return 42\n", target.read_text(encoding="utf-8"))
        change_set = journal.seal_task(("src/app.py",), "not-run")
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/app.py",), change_set.tainted_paths)
        metadata = target.stat()
        self.assertEqual(
            FileIdentity(metadata.st_dev, metadata.st_ino),
            change_set.changes[0].after.identity,
        )
        with self.assertRaisesRegex(ChangeJournalError, "src/app.py"):
            render_change_set_diff(change_set)

    def test_create_taints_expected_snapshot_when_post_commit_read_fails(self) -> None:
        """硬链接发布后无法核验公开目标时不得产生可预览、可撤销账本。"""
        target = self.workspace / "src" / "read-failed-create.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        real_snapshot = ToolHandler._snapshot

        def fail_published_target_read(
            binding: object,
            name: str,
            relative: str,
        ) -> object:
            if name == target.name and target.exists():
                raise OSError("CREATE-POST-COMMIT-PRIVATE-SENTINEL")
            return real_snapshot(binding, name, relative)  # type: ignore[arg-type]

        with patch.object(
                ToolHandler,
                "_snapshot",
            side_effect=fail_published_target_read,
        ):
            result = self.registry.execute(
                "create_file",
                {"path": "src/read-failed-create.py", "content": "created\n"},
            )

        self.assertFalse(result.ok)
        self.assertEqual(
            "文件发布后状态无法验证：src/read-failed-create.py",
            result.output,
        )
        self.assertEqual("created\n", target.read_text(encoding="utf-8"))
        change_set = journal.seal_task(("src/read-failed-create.py",), "not-run")
        self.assertIsNotNone(change_set)
        assert change_set is not None
        self.assertEqual(("src/read-failed-create.py",), change_set.tainted_paths)
        self.assertEqual("created\n", change_set.changes[0].after.content)

    def test_create_reports_cleanup_warning_when_unverified_publish_link_remains(
        self,
    ) -> None:
        """发布后核验与临时硬链接清理同时失败时必须公开固定清理警告。"""
        target = self.workspace / "src" / "unverified-cleanup.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        real_snapshot = ToolHandler._snapshot

        def fail_published_target_read(
            binding: object,
            name: str,
            relative: str,
        ) -> object:
            if name == target.name and target.exists():
                raise OSError("UNVERIFIED-CLEANUP-PRIVATE-SENTINEL")
            return real_snapshot(binding, name, relative)  # type: ignore[arg-type]

        with patch_binding_cleanup_failure(target):
            with patch.object(
                ToolHandler,
                "_snapshot",
                side_effect=fail_published_target_read,
            ):
                result = self.registry.execute(
                    "create_file",
                    {"path": "src/unverified-cleanup.py", "content": "created\n"},
                )

        self.assertFalse(result.ok)
        self.assertIn("文件发布后状态无法验证：src/unverified-cleanup.py", result.output)
        self.assertIn("清理警告", result.output)
        self.assertNotIn("UNVERIFIED-CLEANUP-PRIVATE-SENTINEL", result.output)
        self.assertEqual(1, len(list((self.workspace / "src").glob(".unverified-cleanup.py.*.tmp"))))

    def test_create_reports_success_with_warning_after_binding_close_failure(
        self,
    ) -> None:
        """硬链接发布成功后关闭目录绑定失败，不得把真实创建伪装成失败。"""
        target = self.workspace / "src" / "close-warning.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal

        with patch_binding_close_failure():
            result = self.registry.execute(
                "create_file",
                {
                    "path": "src/close-warning.py",
                    "content": "committed = True\n",
                },
            )

        self.assertTrue(result.ok)
        self.assertIn("关闭警告", result.output)
        self.assertEqual(
            "committed = True\n",
            target.read_text(encoding="utf-8"),
        )
        change_set = journal.seal_task(("src/close-warning.py",), "not-run")
        self.assertEqual("src/close-warning.py", change_set.changes[0].path)

    def test_create_reports_success_with_warning_when_post_commit_record_fails(
        self,
    ) -> None:
        """硬链接发布后的账本异常不得把已提交文件伪装成失败。"""
        target = self.workspace / "src" / "record-warning.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal

        with patch.object(
            ToolHandler,
            "_record_committed",
            side_effect=RuntimeError("simulated post-commit record failure"),
        ):
            result = self.registry.execute(
                "create_file",
                {
                    "path": "src/record-warning.py",
                    "content": "committed = True\n",
                },
            )

        self.assertTrue(result.ok, result.output)
        self.assertEqual("src/record-warning.py", result.relative_path)
        self.assertIn("账本警告", result.output)
        self.assertEqual("committed = True\n", target.read_text(encoding="utf-8"))
        self.assertIsNone(journal.seal_task(("src/record-warning.py",), "not-run"))

    def test_create_file_rejects_sensitive_path(self) -> None:
        """防止创建工具绕过工作区敏感路径策略。"""
        result = self.registry.execute(
            "create_file",
            {"path": ".local/unsafe.txt", "content": "blocked\n"},
        )

        self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)

    def test_read_only_mode_rejects_mutating_tools(self) -> None:
        """防止只读模式通过编辑或命令工具产生副作用。"""
        read_only = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                self.approver,
                read_only=True,
            )
        )

        edit = read_only.execute(
            "edit_file",
            {"path": "src/app.py", "old_text": "41", "new_text": "42"},
        )
        command = read_only.execute("run_command", {"command": "python -m unittest"})
        create = read_only.execute(
            "create_file",
            {"path": "src/forbidden.py", "content": "blocked = True\n"},
        )

        self.assertFalse(edit.ok)
        self.assertFalse(command.ok)
        self.assertFalse(create.ok)
        self.assertFalse((self.workspace / "src" / "forbidden.py").exists())
        self.assertEqual([], self.approver.requests)

    @staticmethod
    def _file_snapshot(path: Path, relative_path: str) -> FileSnapshot:
        metadata = path.stat()
        return FileSnapshot(
            relative_path,
            path.read_text(encoding="utf-8"),
            stat.S_IMODE(metadata.st_mode),
            FileIdentity(metadata.st_dev, metadata.st_ino),
        )

    @staticmethod
    def _change_set(*changes: FileChange) -> TaskChangeSet:
        return TaskChangeSet(
            changes=tuple(changes),
            before_modified_files=("before.py",),
            before_verification="not-run",
            after_modified_files=tuple(change.path for change in changes),
            after_verification="passed",
        )

    def test_preview_and_undo_restore_modified_file_content_and_mode(self) -> None:
        target = self.workspace / "src" / "app.py"
        os.chmod(target, 0o600)
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("def answer():\n    return 42\n", encoding="utf-8")
        os.chmod(target, 0o400)
        after = self._file_snapshot(target, "src/app.py")
        change_set = self._change_set(FileChange("src/app.py", before, after))

        preview = self.registry.preview_undo(change_set)
        execution = self.registry.undo_change_set(change_set)

        self.assertEqual(("src/app.py",), preview.paths)
        self.assertIn("-    return 42", preview.diff)
        self.assertIn("+    return 41", preview.diff)
        self.assertTrue(execution.ok, execution)
        self.assertEqual(before.content, target.read_text(encoding="utf-8"))
        self.assertEqual(before.mode, stat.S_IMODE(target.stat().st_mode))

    def test_preview_closes_opened_bindings_when_later_parent_open_fails(self) -> None:
        """防止第二个父目录绑定失败时泄漏此前已打开的绑定。"""
        other_parent = self.workspace / "z-other"
        other_parent.mkdir()
        other = other_parent / "other.py"
        other.write_text("after\n", encoding="utf-8")
        app = self.workspace / "src" / "app.py"
        change_set = self._change_set(
            FileChange("src/app.py", None, self._file_snapshot(app, "src/app.py")),
            FileChange(
                "z-other/other.py",
                None,
                self._file_snapshot(other, "z-other/other.py"),
            ),
        )
        real_open = binding_module._DirectoryBinding.open
        state = {"closed": False}
        opened: list[object] = []

        class TrackingBinding:
            def __init__(self, binding: object) -> None:
                self.binding = binding

            def __getattr__(self, name: str) -> object:
                return getattr(self.binding, name)

            def close(self) -> None:
                self.binding.close()  # type: ignore[attr-defined]
                state["closed"] = True

        def fail_second_open(workspace: Path, parent: Path) -> object:
            # Windows 临时目录可能同时出现短路径和规范路径；按目录身份判断，
            # 避免同一目录仅因文本表示不同而漏掉故障注入。
            if parent.samefile(other_parent):
                raise OSError("SECOND-OPEN-SENTINEL")
            binding = TrackingBinding(real_open(workspace, parent))
            opened.append(binding)
            return binding

        try:
            with patch.object(
                tools_module._DirectoryBinding,
                "open",
                side_effect=fail_second_open,
            ):
                with self.assertRaisesRegex(OSError, "SECOND-OPEN-SENTINEL"):
                    self.registry.preview_undo(change_set)
            self.assertTrue(state["closed"])
        finally:
            if opened and not state["closed"]:
                opened[0].close()  # type: ignore[attr-defined]

    def test_undo_mixed_update_and_creation_deletes_created_file(self) -> None:
        updated = self.workspace / "src" / "app.py"
        before = self._file_snapshot(updated, "src/app.py")
        updated.write_text("def answer():\n    return 42\n", encoding="utf-8")
        after = self._file_snapshot(updated, "src/app.py")
        created = self.workspace / "src" / "created.py"
        created.write_text("CREATED_SENTINEL = True\n", encoding="utf-8")
        created_after = self._file_snapshot(created, "src/created.py")
        change_set = self._change_set(
            FileChange("src/created.py", None, created_after),
            FileChange("src/app.py", before, after),
        )

        execution = self.registry.undo_change_set(change_set)

        self.assertTrue(execution.ok)
        self.assertEqual(("src/app.py", "src/created.py"), execution.paths)
        self.assertEqual(before.content, updated.read_text(encoding="utf-8"))
        self.assertFalse(created.exists())

    def test_partial_undo_restores_deleted_created_file_from_verified_backup(self) -> None:
        """防止先删任务新文件后，后续发布失败让补偿因公开目标缺失而失败。"""
        created = self.workspace / "src" / "aaa-created.py"
        created.write_text("created-after\n", encoding="utf-8")
        created_after = self._file_snapshot(created, "src/aaa-created.py")
        other = self.workspace / "src" / "other.py"
        other.write_text("other-before\n", encoding="utf-8")
        other_before = self._file_snapshot(other, "src/other.py")
        other.write_text("other-after\n", encoding="utf-8")
        other_after = self._file_snapshot(other, "src/other.py")
        change_set = self._change_set(
            FileChange("src/aaa-created.py", None, created_after),
            FileChange("src/other.py", other_before, other_after),
        )

        with patch_binding_publish_failure():
            execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertEqual(
            created_after,
            self._file_snapshot(created, "src/aaa-created.py"),
        )
        self.assertEqual(other_after, self._file_snapshot(other, "src/other.py"))

    def test_undo_content_conflict_makes_entire_change_set_zero_write(self) -> None:
        updated = self.workspace / "src" / "app.py"
        before = self._file_snapshot(updated, "src/app.py")
        updated.write_text("def answer():\n    return 42\n", encoding="utf-8")
        after = self._file_snapshot(updated, "src/app.py")
        created = self.workspace / "src" / "created.py"
        created.write_text("after\n", encoding="utf-8")
        created_after = self._file_snapshot(created, "src/created.py")
        change_set = self._change_set(
            FileChange("src/app.py", before, after),
            FileChange("src/created.py", None, created_after),
        )
        created.write_text("external\n", encoding="utf-8")

        execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual(("src/created.py",), execution.conflicts)
        self.assertEqual(after.content, updated.read_text(encoding="utf-8"))
        self.assertEqual("external\n", created.read_text(encoding="utf-8"))

    def test_undo_existence_conflict_makes_entire_change_set_zero_write(self) -> None:
        updated = self.workspace / "src" / "app.py"
        before = self._file_snapshot(updated, "src/app.py")
        updated.write_text("def answer():\n    return 42\n", encoding="utf-8")
        after = self._file_snapshot(updated, "src/app.py")
        created = self.workspace / "src" / "created.py"
        created.write_text("after\n", encoding="utf-8")
        created_after = self._file_snapshot(created, "src/created.py")
        change_set = self._change_set(
            FileChange("src/app.py", before, after),
            FileChange("src/created.py", None, created_after),
        )
        created.unlink()

        execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual(("src/created.py",), execution.conflicts)
        self.assertEqual(after.content, updated.read_text(encoding="utf-8"))
        self.assertFalse(created.exists())

    def test_undo_rejects_same_content_with_replaced_identity(self) -> None:
        target = self.workspace / "src" / "app.py"
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("def answer():\n    return 42\n", encoding="utf-8")
        after = self._file_snapshot(target, "src/app.py")
        replacement = self.workspace / "src" / "replacement.py"
        replacement.write_text(after.content, encoding="utf-8")
        os.chmod(replacement, after.mode)
        os.replace(replacement, target)

        execution = self.registry.undo_change_set(
            self._change_set(FileChange("src/app.py", before, after))
        )

        self.assertFalse(execution.ok)
        self.assertEqual(("src/app.py",), execution.conflicts)
        self.assertEqual(after.content, target.read_text(encoding="utf-8"))

    def test_undo_rejects_mode_change_and_rechecks_after_preview(self) -> None:
        target = self.workspace / "src" / "app.py"
        os.chmod(target, 0o600)
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("def answer():\n    return 42\n", encoding="utf-8")
        after = self._file_snapshot(target, "src/app.py")
        change_set = self._change_set(FileChange("src/app.py", before, after))
        self.registry.preview_undo(change_set)
        changed_mode = 0o400 if after.mode != 0o400 else 0o600
        os.chmod(target, changed_mode)
        changed_mode = stat.S_IMODE(target.stat().st_mode)

        execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual(("src/app.py",), execution.conflicts)
        self.assertEqual(after.content, target.read_text(encoding="utf-8"))
        self.assertEqual(changed_mode, stat.S_IMODE(target.stat().st_mode))

    def test_partial_undo_failure_compensates_prior_file_back_to_exact_after(self) -> None:
        app = self.workspace / "src" / "app.py"
        app_before = self._file_snapshot(app, "src/app.py")
        app.write_text("app-after\n", encoding="utf-8")
        app_after = self._file_snapshot(app, "src/app.py")
        other = self.workspace / "src" / "other.py"
        other.write_text("other-before\n", encoding="utf-8")
        other_before = self._file_snapshot(other, "src/other.py")
        other.write_text("other-after\n", encoding="utf-8")
        other_after = self._file_snapshot(other, "src/other.py")
        change_set = self._change_set(
            FileChange("src/app.py", app_before, app_after),
            FileChange("src/other.py", other_before, other_after),
        )

        with patch_binding_publish_failure():
            execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertEqual(app_after, self._file_snapshot(app, "src/app.py"))
        self.assertEqual(other_after, self._file_snapshot(other, "src/other.py"))

    def test_partial_undo_reports_path_when_safe_compensation_cannot_publish(self) -> None:
        app = self.workspace / "src" / "app.py"
        app_before = self._file_snapshot(app, "src/app.py")
        app.write_text("app-after\n", encoding="utf-8")
        app_after = self._file_snapshot(app, "src/app.py")
        other = self.workspace / "src" / "other.py"
        other.write_text("other-before\n", encoding="utf-8")
        other_before = self._file_snapshot(other, "src/other.py")
        other.write_text("other-after\n", encoding="utf-8")
        other_after = self._file_snapshot(other, "src/other.py")
        change_set = self._change_set(
            FileChange("src/app.py", app_before, app_after),
            FileChange("src/other.py", other_before, other_after),
        )

        with patch_binding_publish_failure(rollback_fails=True):
            execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual(("src/app.py",), execution.compensation_failed)
        self.assertEqual(app_before.content, app.read_text(encoding="utf-8"))
        self.assertEqual(other_after, self._file_snapshot(other, "src/other.py"))

    @unittest.skipUnless(os.name == "nt", "仅 Windows 会在覆盖只读目标前主动解锁")
    def test_failure_after_unlocking_read_only_target_restores_exact_after_snapshot(self) -> None:
        target = self.workspace / "src" / "app.py"
        os.chmod(target, 0o600)
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("after\n", encoding="utf-8")
        os.chmod(target, 0o400)
        after = self._file_snapshot(target, "src/app.py")
        change_set = self._change_set(FileChange("src/app.py", before, after))

        with patch_post_chmod_read_failure():
            execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertEqual(after, self._file_snapshot(target, "src/app.py"))

    def test_posix_binding_chmod_uses_nofollow_descriptor_and_fchmod(self) -> None:
        binding = object.__new__(binding_module._PosixDirectoryBinding)
        binding._fd = 71
        with (
            patch.object(binding_module.os, "O_NOFOLLOW", 0x200000, create=True),
            patch.object(binding_module.os, "open", return_value=72) as opened,
            patch.object(binding_module.os, "fchmod", create=True) as fchmod,
            patch.object(binding_module.os, "close") as closed,
            patch.object(binding_module.os, "chmod") as path_chmod,
        ):
            binding.chmod("app.py", 0o640)

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | 0x200000
        opened.assert_called_once_with("app.py", flags, dir_fd=71)
        fchmod.assert_called_once_with(72, 0o640)
        closed.assert_called_once_with(72)
        path_chmod.assert_not_called()

    def test_posix_binding_requires_fchmod_capability_before_opening(self) -> None:
        supported = {
            os.chmod,
            os.rename,
            os.open,
            os.link,
            os.unlink,
            os.stat,
        }
        with (
            patch.object(binding_module, "_is_windows", return_value=False),
            patch.object(binding_module.os, "supports_dir_fd", supported),
            patch.object(binding_module.os, "fchmod", None, create=True),
            patch.object(binding_module, "_PosixDirectoryBinding") as constructor,
        ):
            with self.assertRaises(tools_module.PolicyError):
                binding_module._DirectoryBinding.open(self.workspace, self.workspace / "src")

        constructor.assert_not_called()

    def test_posix_binding_rejects_zero_nofollow_flag_before_opening(self) -> None:
        supported = {os.rename, os.open, os.link, os.unlink, os.stat}
        with (
            patch.object(binding_module, "_is_windows", return_value=False),
            patch.object(binding_module.os, "supports_dir_fd", supported),
            patch.object(binding_module.os, "fchmod", create=True),
            patch.object(binding_module.os, "O_NOFOLLOW", 0, create=True),
            patch.object(binding_module, "_PosixDirectoryBinding") as constructor,
        ):
            with self.assertRaises(tools_module.PolicyError):
                binding_module._DirectoryBinding.open(self.workspace, self.workspace / "src")

        constructor.assert_not_called()

    def test_posix_binding_rejects_missing_nofollow_flag_before_opening(self) -> None:
        supported = {os.rename, os.open, os.link, os.unlink, os.stat}
        with (
            patch.object(binding_module, "_is_windows", return_value=False),
            patch.object(binding_module.os, "supports_dir_fd", supported),
            patch.object(binding_module.os, "fchmod", create=True),
            patch.dict(binding_module.os.__dict__, {}, clear=False),
            patch.object(binding_module, "_PosixDirectoryBinding") as constructor,
        ):
            binding_module.os.__dict__.pop("O_NOFOLLOW", None)
            with self.assertRaises(tools_module.PolicyError):
                binding_module._DirectoryBinding.open(self.workspace, self.workspace / "src")

        constructor.assert_not_called()

    def test_posix_update_undo_does_not_chmod_target(self) -> None:
        target = self.workspace / "src" / "app.py"
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("after\n", encoding="utf-8")
        after = self._file_snapshot(target, "src/app.py")

        with (
            patch_posix_semantics_binding(),
            patch.object(binding_module, "_is_windows", return_value=False),
        ):
            execution = self.registry.undo_change_set(
                self._change_set(FileChange("src/app.py", before, after))
            )

        self.assertTrue(execution.ok)
        self.assertEqual(before.content, target.read_text(encoding="utf-8"))

    def test_posix_created_read_only_file_undo_does_not_chmod_target(self) -> None:
        target = self.workspace / "src" / "created.py"
        target.write_text("created\n", encoding="utf-8")
        os.chmod(target, 0o400)
        after = self._file_snapshot(target, "src/created.py")

        with (
            patch_posix_semantics_binding(),
            patch.object(binding_module, "_is_windows", return_value=False),
        ):
            execution = self.registry.undo_change_set(
                self._change_set(FileChange("src/created.py", None, after))
            )

        self.assertTrue(execution.ok)
        self.assertFalse(target.exists())

    def test_posix_read_only_update_undo_does_not_chmod_target(self) -> None:
        target = self.workspace / "src" / "app.py"
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("after\n", encoding="utf-8")
        os.chmod(target, 0o400)
        after = self._file_snapshot(target, "src/app.py")

        with (
            patch_posix_semantics_binding(),
            patch.object(binding_module, "_is_windows", return_value=False),
        ):
            execution = self.registry.undo_change_set(
                self._change_set(FileChange("src/app.py", before, after))
            )

        self.assertTrue(execution.ok)
        self.assertEqual(before.content, target.read_text(encoding="utf-8"))
        self.assertEqual(before.mode, stat.S_IMODE(target.stat().st_mode))

    def test_external_replace_after_backup_link_is_not_chmodded_or_overwritten(self) -> None:
        target = self.workspace / "src" / "app.py"
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("after\n", encoding="utf-8")
        os.chmod(target, 0o400)
        after = self._file_snapshot(target, "src/app.py")
        replacement = self.workspace / "src" / "external.py"
        replacement.write_text("external\n", encoding="utf-8")
        os.chmod(replacement, 0o400)
        external = self._file_snapshot(replacement, "src/app.py")
        probes: list[ReplaceAfterBackupLinkBinding] = []

        with patch_replace_after_backup_link(
            replacement,
            after.mode,
            external.mode,
            probes,
        ):
            execution = self.registry.undo_change_set(
                self._change_set(FileChange("src/app.py", before, after))
            )

        self.assertFalse(execution.ok)
        self.assertTrue(probes[0].replaced, execution)
        self.assertNotIn("app.py", probes[0].chmod_names)
        self.assertEqual(external, self._file_snapshot(target, "src/app.py"))

    def test_snapshot_failure_after_successful_replace_compensates_exact_after(self) -> None:
        target = self.workspace / "src" / "app.py"
        before = self._file_snapshot(target, "src/app.py")
        target.write_text("after\n", encoding="utf-8")
        after = self._file_snapshot(target, "src/app.py")

        with patch_read_failure_after_successful_replace():
            execution = self.registry.undo_change_set(
                self._change_set(FileChange("src/app.py", before, after))
            )

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertEqual(after, self._file_snapshot(target, "src/app.py"))

    def test_undo_recreate_failure_before_link_keeps_after_missing_without_compensation_error(
        self,
    ) -> None:
        target = self.workspace / "src" / "deleted.py"
        target.write_text("before deletion\n", encoding="utf-8")
        before = self._file_snapshot(target, "src/deleted.py")
        target.unlink()
        journal = ChangeJournal()
        journal.begin_task(("src/deleted.py",), "passed")
        journal.record_committed("src/deleted.py", before, None)
        change_set = journal.seal_task((), "passed")
        self.assertIsNotNone(change_set)
        self.registry.context.change_journal = journal

        with patch_undo_create_publish_failure("before"):
            execution = self.registry.undo_change_set(change_set)  # type: ignore[arg-type]

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertFalse(target.exists())
        self.assertIs(change_set, journal.latest())

    def test_undo_recreate_snapshot_failure_after_link_removes_published_file_cleanly(
        self,
    ) -> None:
        target = self.workspace / "src" / "deleted.py"
        target.write_text("before deletion\n", encoding="utf-8")
        before = self._file_snapshot(target, "src/deleted.py")
        target.unlink()
        journal = ChangeJournal()
        journal.begin_task(("src/deleted.py",), "passed")
        journal.record_committed("src/deleted.py", before, None)
        change_set = journal.seal_task((), "passed")
        self.assertIsNotNone(change_set)
        self.registry.context.change_journal = journal

        with patch_undo_create_publish_failure("after"):
            execution = self.registry.undo_change_set(change_set)  # type: ignore[arg-type]

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertFalse(target.exists())
        self.assertIs(change_set, journal.latest())

    def test_approved_command_runs_without_shell(self) -> None:
        """防止合法命令经 Shell 拼接造成意外代码执行。"""
        result = self.registry.execute(
            "run_command",
            {"command": "python -m compileall -q src"},
        )

        self.assertTrue(result.ok)
        self.assertIn("退出码：0", result.output)
        self.assertEqual("run_command", self.approver.requests[0][0])

    def test_git_toplevel_uses_policy_executable_and_filtered_environment(self) -> None:
        """Repository probing must not run a naked Git before approval."""
        policy = CommandPolicy(self.workspace)
        trusted_git = policy.validate("git status")[0]
        completed = subprocess.CompletedProcess(
            [trusted_git],
            0,
            stdout=str(self.workspace),
            stderr="",
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private"}):
            with patch.object(
                command_module.subprocess,
                "run",
                return_value=completed,
            ) as run:
                root = command_module._git_toplevel(self.workspace, policy)

        self.assertEqual(self.workspace.resolve(), root)
        called_args = run.call_args.args[0]
        called_env = run.call_args.kwargs["env"]
        self.assertEqual(trusted_git, called_args[0])
        self.assertNotIn("OPENAI_API_KEY", called_env)

    def test_filtered_env_drops_sensitive_variables(self) -> None:
        """子进程环境必须剔除 API Key/token/password 等敏感变量。"""
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "k1",
                "DEEPSEEK_API_TOKEN": "t1",
                "PGPASSWORD": "p1",
                "MYAPP_SECRET": "s1",
                "AWS_ACCESS_KEY": "a1",
            },
        ):
            env = command_module._filtered_env()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("DEEPSEEK_API_TOKEN", env)
        self.assertNotIn("PGPASSWORD", env)
        self.assertNotIn("MYAPP_SECRET", env)
        self.assertNotIn("AWS_ACCESS_KEY", env)
        # 必要系统/构建变量被显式保留
        self.assertIn("PATH", env)

    def test_approved_command_receives_filtered_environment(self) -> None:
        """实际执行验证：子进程读不到被剔除的 API Key。"""
        probe = "import os;print('FOUND' if 'OPENAI_API_KEY' in os.environ else 'GONE')"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k1"}):
            (self.workspace / "probe.py").write_text(probe, encoding="utf-8")
            result = self.registry.execute(
                "run_command",
                {"command": "python probe.py"},
            )
        self.assertIn("GONE", result.output)

    def test_run_command_terminates_process_tree_when_output_exceeds_limit(self) -> None:
        """Output limiting must be an in-memory boundary, not post-capture slicing."""
        (self.workspace / "flood.py").write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "sys.stdout.write('x' * 5_000_000)\n"
            "sys.stdout.flush()\n"
            "Path('after-flood.txt').write_text('should-not-exist')\n",
            encoding="utf-8",
        )
        self.registry.context.max_output_chars = 1024

        result = self.registry.execute(
            "run_command",
            {"command": "python flood.py"},
        )

        self.assertFalse(result.ok, result.output)
        self.assertIn("输出超过", result.output)
        self.assertLessEqual(len(result.output), 1200)
        self.assertFalse((self.workspace / "after-flood.txt").exists())

    def test_run_command_timeout_terminates_descendant_processes(self) -> None:
        """A timed-out command must not leave a child process running."""
        child_code = (
            "import time; from pathlib import Path; "
            "time.sleep(0.6); Path('child-alive.txt').write_text('alive')"
        )
        (self.workspace / "spawn_child.py").write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
        registry = ToolRegistry(
            ToolContext(
                workspace_policy=WorkspacePolicy(self.workspace),
                command_policy=CommandPolicy(self.workspace),
                approver=lambda _action, _detail: True,
                timeout=0.2,
            )
        )

        result = registry.execute(
            "run_command",
            {"command": "python spawn_child.py"},
        )
        time.sleep(0.9)

        self.assertFalse(result.ok)
        self.assertIn("超过 0.2 秒", result.output)
        self.assertFalse((self.workspace / "child-alive.txt").exists())


if __name__ == "__main__":
    unittest.main()
