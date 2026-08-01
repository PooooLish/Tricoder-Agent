import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder import tools as tools_module
from tricoder.changes import ChangeJournal, FileIdentity
from tricoder.models import ToolDefinition
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry


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
    real_open = tools_module._DirectoryBinding.open

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
    real_open = tools_module._DirectoryBinding.open

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
    real_open = tools_module._DirectoryBinding.open

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
                "edit_file",
                "create_file",
                "apply_patch",
                "run_command",
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
                {"path": {"type": "string"}, "query": {"type": "string"}},
                ["query"],
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

        result = read_only.execute("apply_patch", {"patch": "not-a-patch"})

        self.assertFalse(result.ok)
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

        with patch("tricoder.tools.os.link", side_effect=OSError("hard links unavailable")):
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
            patch("tricoder.tools._is_windows", return_value=False, create=True),
            patch("tricoder.tools.os.supports_dir_fd", limited_support),
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

    def test_edit_reports_success_and_records_saved_snapshot_when_post_commit_read_fails(
        self,
    ) -> None:
        """原子替换后的回读失败不得掩盖提交或丢失可用的真实快照。"""
        target = self.workspace / "src" / "app.py"
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        self.registry.context.change_journal = journal
        real_snapshot = ToolRegistry._snapshot

        def fail_published_target_read(
            binding: object,
            name: str,
            relative: str,
        ) -> object:
            if name == "app.py" and "return 42" in target.read_text(encoding="utf-8"):
                raise OSError("simulated post-commit snapshot failure")
            return real_snapshot(binding, name, relative)  # type: ignore[arg-type]

        with patch.object(
            ToolRegistry,
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

        self.assertTrue(result.ok, result.output)
        self.assertEqual("src/app.py", result.relative_path)
        self.assertIn("账本警告", result.output)
        self.assertEqual("def answer():\n    return 42\n", target.read_text(encoding="utf-8"))
        change_set = journal.seal_task(("src/app.py",), "not-run")
        self.assertIsNotNone(change_set)
        assert change_set is not None
        metadata = target.stat()
        self.assertEqual(
            FileIdentity(metadata.st_dev, metadata.st_ino),
            change_set.changes[0].after.identity,
        )

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
            self.registry,
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

    def test_approved_command_runs_without_shell(self) -> None:
        """防止合法检查命令在审批后仍无法执行。"""
        result = self.registry.execute(
            "run_command",
            {"command": "python -m compileall -q src"},
        )

        self.assertTrue(result.ok)
        self.assertIn("退出码：0", result.output)
        self.assertEqual("run_command", self.approver.requests[0][0])


if __name__ == "__main__":
    unittest.main()
