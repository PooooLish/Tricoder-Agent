import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder import tools as tools_module
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
                "run_command",
                "finish",
            ),
            tuple(definition.name for definition in definitions),
        )
        self.assertTrue(all(isinstance(item, ToolDefinition) for item in definitions))
        self.assertIs(definitions, self.registry.definitions)
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

        self.assertIs(definition, self.registry.describe(definition.name))
        self.assertIsNone(self.registry.describe("unknown_tool"))

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
        real_unlink = os.unlink

        def fail_committed_temp(path: object, *args: object, **kwargs: object) -> None:
            if target.exists() and str(path).endswith(".tmp"):
                raise PermissionError("simulated cleanup failure")
            real_unlink(path, *args, **kwargs)

        with patch("tricoder.tools.os.unlink", side_effect=fail_committed_temp):
            result = self.registry.execute(
                "create_file",
                {"path": "src/committed.py", "content": "committed = True\n"},
            )

        self.assertTrue(result.ok)
        self.assertIn("清理警告", result.output)
        self.assertEqual("committed = True\n", target.read_text(encoding="utf-8"))

    def test_edit_reports_success_with_warning_after_binding_close_failure(self) -> None:
        """原子替换成功后关闭目录绑定失败，不得把真实修改伪装成失败。"""
        target = self.workspace / "src" / "app.py"

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

    def test_create_reports_success_with_warning_after_binding_close_failure(
        self,
    ) -> None:
        """硬链接发布成功后关闭目录绑定失败，不得把真实创建伪装成失败。"""
        target = self.workspace / "src" / "close-warning.py"

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
