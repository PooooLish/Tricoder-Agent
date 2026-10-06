import tempfile
import unittest
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.changes import ChangeJournal
from tricoder.execution_state import ErrorCode
from tricoder.models import ProviderResponse, SessionContext, ToolCall
from tricoder.models import AppConfig, ProviderConfig
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.binding import _DirectoryBinding
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore
from tricoder.workspace.snapshot import (
    SnapshotLimits,
    capture_workspace_baseline,
    task_changes_match_baselines,
)
from tests.test_agent import ScriptedProvider, StructuredScriptedProvider


class RecordingApprover:
    """记录审批请求，便于断言修改前后的授权边界。"""

    def __init__(self, *decisions: bool) -> None:
        self.decisions = list(decisions)
        self.requests: list[tuple[str, str]] = []

    def __call__(self, action: str, detail: str) -> bool:
        self.requests.append((action, detail))
        return self.decisions.pop(0)


class DirectoryToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.approver = RecordingApprover(True)
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                self.approver,
            )
        )

    def test_create_directory_creates_nested_directory_after_one_approval(self) -> None:
        """生产代码若未注册并执行专用目录工具，本用例必须失败。"""

        result = self.registry.execute(
            "create_directory",
            {"path": "game/src", "parents": True, "exist_ok": True},
        )

        self.assertTrue(result.ok, result.output)
        self.assertTrue((self.workspace / "game" / "src").is_dir())
        self.assertEqual(1, len(self.approver.requests))
        self.assertEqual("create_directory", self.approver.requests[0][0])

    def test_create_file_can_explicitly_create_missing_parents(self) -> None:
        """create_parents=true 必须走同一次审批并创建真实文件。"""

        result = self.registry.execute(
            "create_file",
            {
                "path": "game/main.py",
                "content": "def main():\n    return 'ok'\n",
                "create_parents": True,
            },
        )

        self.assertTrue(result.ok, result.output)
        self.assertEqual(
            "def main():\n    return 'ok'\n",
            (self.workspace / "game" / "main.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(1, len(self.approver.requests))
        self.assertEqual("create_file", self.approver.requests[0][0])
        self.assertIn("game/", self.approver.requests[0][1].replace("\\", "/"))
        self.assertIn("+def main", self.approver.requests[0][1])

    def test_create_file_parent_approval_rejection_has_zero_side_effects(self) -> None:
        """联合审批被拒绝时，目录、文件和临时文件都不能出现。"""
        self.approver.decisions = [False]

        result = self.registry.execute(
            "create_file",
            {
                "path": "game/main.py",
                "content": "value = 1\n",
                "create_parents": True,
            },
        )

        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "game").exists())

    def test_create_file_publish_failure_compensates_created_parents(self) -> None:
        """文件原子发布失败时，应只移除本次创建且仍为空的父目录。"""
        with patch(
            "tricoder.tools.binding.os.link",
            side_effect=OSError("simulated hard-link failure"),
        ):
            result = self.registry.execute(
                "create_file",
                {
                    "path": "game/src/main.py",
                    "content": "value = 1\n",
                    "create_parents": True,
                },
            )

        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "game").exists())
        self.assertEqual((), result.file_effects.paths)
        self.assertEqual((), result.file_effects.directory_paths)

    def test_create_file_publish_and_temp_cleanup_failure_is_unknown(self) -> None:
        """发布与临时文件清理同时失败时，不得伪装成零副作用。"""
        probe = _DirectoryBinding.open(self.workspace, self.workspace)
        binding_type = type(probe)
        probe.close()

        with (
            patch.object(
                binding_type,
                "link",
                side_effect=OSError("simulated publish failure"),
            ),
            patch.object(
                binding_type,
                "unlink",
                side_effect=OSError("simulated cleanup failure"),
            ),
        ):
            result = self.registry.execute(
                "create_file",
                {"path": "main.py", "content": "value = 1\n"},
            )

        self.assertFalse(result.ok)
        self.assertEqual("unknown", result.file_effects.state.value)
        self.assertEqual(("main.py",), result.file_effects.paths)

    def test_create_file_rejects_parent_created_during_approval(self) -> None:
        """审批期间出现的目录属于外部状态，不能被当前任务接管或删除。"""
        parent = self.workspace / "game"

        def approve_after_external_create(_action: str, _detail: str) -> bool:
            parent.mkdir()
            return True

        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approve_after_external_create,
            )
        )

        result = registry.execute(
            "create_file",
            {
                "path": "game/main.py",
                "content": "value = 1\n",
                "create_parents": True,
            },
        )

        self.assertFalse(result.ok)
        self.assertTrue(parent.is_dir())
        self.assertFalse((parent / "main.py").exists())

    def test_create_file_parent_budget_is_checked_before_approval(self) -> None:
        """目录预算不足不能先弹审批或留下部分目录。"""
        journal = ChangeJournal(max_directories=0)
        journal.begin_task((), "未运行")
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                self.approver,
                change_journal=journal,
            )
        )

        result = registry.execute(
            "create_file",
            {
                "path": "game/main.py",
                "content": "value = 1\n",
                "create_parents": True,
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.POLICY_DENIED, result.error.code)
        self.assertEqual([], self.approver.requests)
        self.assertFalse((self.workspace / "game").exists())

    def test_create_file_keeps_missing_parent_rejection_by_default(self) -> None:
        """省略新参数时保持旧行为，且修改前不得请求审批。"""

        result = self.registry.execute(
            "create_file",
            {"path": "missing/main.py", "content": "value = 1\n"},
        )

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
        self.assertIn("create_directory", result.output)
        self.assertEqual([], self.approver.requests)
        self.assertFalse((self.workspace / "missing").exists())

    def test_existing_directory_is_idempotent_without_approval_or_ownership(self) -> None:
        """已有目录的幂等成功不能把旧目录据为当前任务创建物。"""
        (self.workspace / "game").mkdir()
        journal = ChangeJournal()
        journal.begin_task((), "未运行")
        self.registry.context.change_journal = journal

        result = self.registry.execute(
            "create_directory",
            {"path": "game", "exist_ok": True},
        )

        self.assertTrue(result.ok, result.output)
        self.assertEqual([], self.approver.requests)
        self.assertIsNone(journal.seal_task((), "未运行"))

    def test_directory_rejects_read_only_even_when_target_exists(self) -> None:
        """write 工具入口的只读边界不能因幂等 no-op 被绕过。"""
        (self.workspace / "game").mkdir()
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                self.approver,
                read_only=True,
            )
        )

        result = registry.execute("create_directory", {"path": "game"})

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.POLICY_DENIED, result.error.code)
        self.assertEqual([], self.approver.requests)

    def test_rejected_directory_creation_leaves_no_partial_chain(self) -> None:
        """审批拒绝必须发生在任何 mkdir 前。"""
        self.approver.decisions = [False]

        result = self.registry.execute(
            "create_directory",
            {"path": "game/src", "parents": True},
        )

        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "game").exists())

    def test_directory_rejects_conflicts_limits_and_forbidden_paths_before_approval(self) -> None:
        """冲突、绝对/敏感路径、禁建父链和层级上限都必须零写入拒绝。"""
        (self.workspace / "file").write_text("x", encoding="utf-8")
        cases = (
            ({"path": "file"},),
            ({"path": "a/b", "parents": False},),
            ({"path": ".env.local/cache"},),
            ({"path": str((self.workspace / "absolute").resolve())},),
            ({"path": "/".join(f"d{i}" for i in range(17))},),
        )
        for (arguments,) in cases:
            with self.subTest(arguments=arguments):
                result = self.registry.execute("create_directory", arguments)
                self.assertFalse(result.ok)
        self.assertEqual([], self.approver.requests)

    def test_directory_exist_ok_false_rejects_existing_target(self) -> None:
        """exist_ok=false 对已有普通目录返回可纠正错误且不审批。"""
        (self.workspace / "game").mkdir()

        result = self.registry.execute(
            "create_directory", {"path": "game", "exist_ok": False}
        )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
        self.assertEqual([], self.approver.requests)

    def test_directory_approval_race_does_not_claim_external_directory(self) -> None:
        """审批期间出现的目标目录使确认失效，并保留外部目录。"""
        target = self.workspace / "game"

        def approve_after_external_create(_action: str, _detail: str) -> bool:
            target.mkdir()
            return True

        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approve_after_external_create,
            )
        )

        result = registry.execute("create_directory", {"path": "game"})

        self.assertFalse(result.ok)
        self.assertTrue(target.is_dir())

    def test_existing_parent_replacement_during_approval_cannot_redirect_creation(self) -> None:
        """审批期间替换同名普通父目录时，写入不能落入替代对象。"""
        parent = self.workspace / "existing"
        moved = self.workspace / "moved"
        parent.mkdir()
        replacement_succeeded = False

        def attempt_parent_replacement(_action: str, _detail: str) -> bool:
            nonlocal replacement_succeeded
            try:
                parent.rename(moved)
                parent.mkdir()
            except OSError:
                # Windows 的无 delete-share 目录句柄会直接阻止替换；POSIX
                # 允许重命名，但审批后身份重检必须在 mkdir 前拒绝。
                return True
            replacement_succeeded = True
            return True

        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                attempt_parent_replacement,
            )
        )

        result = registry.execute(
            "create_directory", {"path": "existing/child"}
        )

        if replacement_succeeded:
            self.assertFalse(result.ok)
            self.assertFalse((parent / "child").exists())
        else:
            self.assertTrue(result.ok, result.output)
            self.assertTrue((parent / "child").is_dir())

    def test_second_directory_creation_failure_compensates_first_layer(self) -> None:
        """逐层创建在第二层失败时，应逆序移除已确认的第一层。"""
        probe = _DirectoryBinding.open(self.workspace, self.workspace)
        binding_type = type(probe)
        probe.close()
        original = binding_type.create_directory

        def fail_second(binding, name):  # type: ignore[no-untyped-def]
            if name == "src":
                raise OSError("simulated second layer failure")
            return original(binding, name)

        with patch.object(binding_type, "create_directory", new=fail_second):
            result = self.registry.execute(
                "create_directory", {"path": "game/src"}
            )

        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "game").exists())
        self.assertEqual(("game/src",), result.file_effects.directory_paths)
        self.assertEqual("unknown", result.file_effects.state.value)

    def test_directory_effects_and_workspace_reconciliation_are_exact(self) -> None:
        """空目录应由独立账本解释；未登记的外部目录仍必须被发现。"""
        limits = SnapshotLimits(timeout_seconds=3.0)
        before = capture_workspace_baseline(self.workspace, limits)
        journal = ChangeJournal()
        journal.begin_task((), "未运行")
        self.registry.context.change_journal = journal

        result = self.registry.execute(
            "create_directory", {"path": "game/src"}
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(("game", "game/src"), result.file_effects.directory_paths)
        change_set = journal.seal_task(
            (), "待验证", result.file_effects.directory_paths
        )
        after = capture_workspace_baseline(self.workspace, limits)
        self.assertTrue(task_changes_match_baselines(before, after, change_set))

        (self.workspace / "external").mkdir()
        with_external = capture_workspace_baseline(self.workspace, limits)
        self.assertFalse(
            task_changes_match_baselines(before, with_external, change_set)
        )

    def test_fake_provider_can_finish_a_pure_directory_task_without_test_evidence(self) -> None:
        """空目录任务可正常完成，但不得伪造测试通过或普通文件修改。"""
        journal = ChangeJournal()
        journal.begin_task((), "未运行")
        self.registry.context.change_journal = journal
        provider = StructuredScriptedProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "mkdir",
                            "create_directory",
                            {"path": "game", "parents": True},
                        ),
                    )
                ),
                ProviderResponse(
                    tool_calls=(
                        ToolCall("finish", "finish", {"summary": "目录已创建"}),
                    )
                ),
            ]
        )
        agent = CodingAgent(
            provider,
            self.registry,
            max_rounds=2,
            plan_enabled=False,
        )

        turn = agent.run_with_context("创建空目录 game", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual((), turn.result.modified_files)
        self.assertEqual(("game",), turn.result.modified_directories)
        self.assertEqual("未运行", turn.result.verification)
        self.assertIsNone(turn.context.verification_evidence)


class DirectoryBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def test_binding_creates_identifies_and_removes_only_expected_empty_directory(self) -> None:
        """平台绑定必须返回真实身份，并只删除身份匹配的空目录。"""
        binding = _DirectoryBinding.open(self.workspace, self.workspace)
        try:
            created = binding.create_directory("game")
            self.assertEqual(created, binding.directory_identity("game"))
            binding.remove_empty_directory("game", created)
        finally:
            binding.close()

        self.assertFalse((self.workspace / "game").exists())


class DirectoryUndoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.approver = RecordingApprover(True, True, True)
        self.journal = ChangeJournal()
        self.journal.begin_task((), "未运行")
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                self.approver,
                change_journal=self.journal,
            )
        )

    def seal(self):  # type: ignore[no-untyped-def]
        effects = self.journal.active_effects()
        return self.journal.seal_task(
            effects.paths,
            "待验证" if effects.paths else "未运行",
            effects.directory_paths,
        )

    def test_undo_removes_pure_created_directory_chain_deepest_first(self) -> None:
        """纯目录任务也必须产生可预览、可执行的最近撤销。"""
        result = self.registry.execute(
            "create_directory", {"path": "game/src"}
        )
        self.assertTrue(result.ok, result.output)
        change_set = self.seal()
        assert change_set is not None

        preview = self.registry.preview_undo(change_set)
        execution = self.registry.undo_change_set(change_set)

        self.assertIn("- directory game/src/", preview.diff)
        self.assertEqual(("game/src", "game"), preview.directory_paths)
        self.assertTrue(execution.ok)
        self.assertFalse((self.workspace / "game").exists())

    def test_undo_created_file_then_removes_only_owned_parents(self) -> None:
        """撤销嵌套文件时先恢复文件，再删除本任务创建的父目录。"""
        result = self.registry.execute(
            "create_file",
            {
                "path": "game/src/main.py",
                "content": "value = 1\n",
                "create_parents": True,
            },
        )
        self.assertTrue(result.ok, result.output)
        change_set = self.seal()
        assert change_set is not None

        execution = self.registry.undo_change_set(change_set)

        self.assertTrue(execution.ok, execution)
        self.assertFalse((self.workspace / "game").exists())

    def test_undo_preflight_rejects_external_content_without_removing_anything(self) -> None:
        """目录中出现未登记内容时必须在整组撤销开始前冲突退出。"""
        result = self.registry.execute(
            "create_directory", {"path": "game/src"}
        )
        self.assertTrue(result.ok, result.output)
        change_set = self.seal()
        assert change_set is not None
        external = self.workspace / "game" / "src" / "user.txt"
        external.write_text("keep\n", encoding="utf-8")

        with self.assertRaises(Exception):
            self.registry.preview_undo(change_set)
        execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertTrue(external.exists())
        self.assertTrue((self.workspace / "game" / "src").is_dir())

    def test_undo_preserves_preexisting_parent(self) -> None:
        """任务前已有的祖先目录永远不能进入目录撤销集合。"""
        (self.workspace / "existing").mkdir()
        result = self.registry.execute(
            "create_directory", {"path": "existing/child"}
        )
        self.assertTrue(result.ok, result.output)
        change_set = self.seal()
        assert change_set is not None

        execution = self.registry.undo_change_set(change_set)

        self.assertTrue(execution.ok)
        self.assertTrue((self.workspace / "existing").is_dir())
        self.assertFalse((self.workspace / "existing" / "child").exists())

    def test_binding_refuses_to_remove_directory_with_user_content(self) -> None:
        """目录补偿绝不递归删除审批后由用户或外部进程写入的内容。"""
        binding = _DirectoryBinding.open(self.workspace, self.workspace)
        try:
            created = binding.create_directory("game")
            (self.workspace / "game" / "user.txt").write_text(
                "keep\n", encoding="utf-8"
            )
            with self.assertRaises(OSError):
                binding.remove_empty_directory("game", created)
        finally:
            binding.close()

        self.assertEqual(
            "keep\n",
            (self.workspace / "game" / "user.txt").read_text(encoding="utf-8"),
        )

    def test_undo_mid_directory_failure_recreates_directories_and_files(self) -> None:
        """最深目录已删除后若祖先删除失败，补偿必须重建目录并恢复文件后态。"""
        result = self.registry.execute(
            "create_file",
            {
                "path": "game/src/main.py",
                "content": "value = 1\n",
                "create_parents": True,
            },
        )
        self.assertTrue(result.ok, result.output)
        change_set = self.seal()
        assert change_set is not None
        probe = _DirectoryBinding.open(self.workspace, self.workspace)
        binding_type = type(probe)
        probe.close()
        original_remove = binding_type.remove_empty_directory

        def fail_outer(binding, name, expected):  # type: ignore[no-untyped-def]
            if name == "game":
                raise OSError("simulated outer removal failure")
            return original_remove(binding, name, expected)

        with patch.object(
            binding_type,
            "remove_empty_directory",
            new=fail_outer,
        ):
            execution = self.registry.undo_change_set(change_set)

        self.assertFalse(execution.ok)
        self.assertEqual((), execution.compensation_failed)
        self.assertEqual(
            "value = 1\n",
            (self.workspace / "game" / "src" / "main.py").read_text(
                encoding="utf-8"
            ),
        )
        refreshed = execution._compensated_change_set
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        original_directories = {
            change.path: change.after for change in change_set.directory_changes
        }
        refreshed_directories = {
            change.path: change.after for change in refreshed.directory_changes
        }
        assert original_directories["game/src"] is not None
        assert refreshed_directories["game/src"] is not None
        self.assertNotEqual(
            original_directories["game/src"].identity,
            refreshed_directories["game/src"].identity,
        )
        retry = self.registry.undo_change_set(refreshed)
        self.assertTrue(retry.ok, retry)
        self.assertFalse((self.workspace / "game").exists())


class DirectoryEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def test_fake_provider_creates_tests_verifies_finishes_and_undoes(self) -> None:
        """覆盖目录、两个文件、真实 unittest、finish 与整组撤销闭环。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            journal = ChangeJournal()
            journal.begin_task((), "未运行")
            registry = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(workspace),
                    CommandPolicy(workspace),
                    RecordingApprover(True, True, True, True),
                    change_journal=journal,
                )
            )
            provider = StructuredScriptedProvider(
                [
                    ProviderResponse(
                        tool_calls=(
                            ToolCall(
                                "mkdir",
                                "create_directory",
                                {"path": "game", "parents": True},
                            ),
                        )
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall(
                                "main",
                                "create_file",
                                {
                                    "path": "game/main.py",
                                    "content": "def add(a, b):\n    return a + b\n",
                                },
                            ),
                        )
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall(
                                "test",
                                "create_file",
                                {
                                    "path": "game/test_main.py",
                                    "content": (
                                        "import unittest\n"
                                        "from main import add\n\n"
                                        "class AddTests(unittest.TestCase):\n"
                                        "    def test_add(self):\n"
                                        "        self.assertEqual(3, add(1, 2))\n\n"
                                        "if __name__ == '__main__':\n"
                                        "    unittest.main()\n"
                                    ),
                                },
                            ),
                        )
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall(
                                "verify",
                                "run_command",
                                {
                                    "command": "python -m unittest discover -s game -v"
                                },
                            ),
                        )
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall(
                                "finish",
                                "finish",
                                {"summary": "game 已创建并通过测试"},
                            ),
                        )
                    ),
                ]
            )
            agent = CodingAgent(
                provider,
                registry,
                max_rounds=5,
                plan_enabled=False,
            )

            turn = agent.run_with_context("创建 game 并测试", SessionContext())

            self.assertTrue(turn.result.ok, turn.result.summary)
            self.assertEqual("通过", turn.result.verification)
            self.assertEqual(("game",), turn.result.modified_directories)
            self.assertEqual(
                ("game/main.py", "game/test_main.py"),
                turn.result.modified_files,
            )
            change_set = journal.seal_task(
                turn.result.modified_files,
                turn.result.verification,
                turn.result.modified_directories,
            )
            assert change_set is not None
            execution = registry.undo_change_set(change_set)
            self.assertTrue(execution.ok, execution)
            self.assertEqual([], list(workspace.iterdir()))

    def test_session_runtime_keeps_directory_state_and_undo_baseline_consistent(self) -> None:
        """真实 Runtime 门禁应接受目录账本，撤销后更新同一 Session 基线。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SessionStore(root / "state.db")
            store.initialize(workspace)
            record = store.create(
                "directory-runtime", workspace, "openai", "test-model"
            )

            def factory(session_record, memory, _options):  # type: ignore[no-untyped-def]
                journal = ChangeJournal()
                registry = ToolRegistry(
                    ToolContext(
                        WorkspacePolicy(workspace),
                        CommandPolicy(workspace),
                        lambda *_args: True,
                        change_journal=journal,
                    )
                )
                provider = StructuredScriptedProvider(
                    [
                        ProviderResponse(
                            tool_calls=(
                                ToolCall(
                                    "mkdir",
                                    "create_directory",
                                    {"path": "game"},
                                ),
                            )
                        ),
                        ProviderResponse(
                            tool_calls=(
                                ToolCall(
                                    "finish",
                                    "finish",
                                    {"summary": "目录创建完成"},
                                ),
                            )
                        ),
                        ProviderResponse(
                            tool_calls=(
                                ToolCall(
                                    "finish-next",
                                    "finish",
                                    {"summary": "后续任务无需重新确认"},
                                ),
                            )
                        ),
                    ]
                )
                config = AppConfig(
                    workspace=workspace,
                    provider=ProviderConfig(
                        "openai",
                        "synthetic-key",
                        "https://example.test",
                        "test-model",
                    ),
                )
                return ActiveSession(
                    session_record,
                    memory,
                    SessionContext(
                        persisted_summary=memory.summary,
                        modified_files=memory.modified_files,
                        verification=memory.verification,
                    ),
                    config,
                    CodingAgent(
                        provider,
                        registry,
                        max_rounds=2,
                        plan_enabled=False,
                    ),
                    registry,
                    journal,
                )

            confirmations: list[object] = []
            runtime = SessionRuntime(
                store,
                workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=factory,
                initial_session_id=record.id,
                workspace_confirmer=lambda preview: (
                    confirmations.append(preview) or True
                ),
            )

            result = runtime.run_task("创建 game 空目录")
            confirmations_after_first = len(confirmations)
            follow_up = runtime.run_task("确认目录仍存在，不修改文件")
            runtime.clear_current()
            self.assertEqual(
                ("game",), runtime.current.context.modified_directories
            )
            preview = runtime.prepare_undo()
            execution = runtime.undo_latest()
            runtime.close()

            self.assertTrue(result.ok, result.summary)
            self.assertTrue(follow_up.ok, follow_up.summary)
            self.assertEqual(confirmations_after_first, len(confirmations))
            self.assertEqual(("game",), result.modified_directories)
            self.assertIn("- directory game/", preview.diff)
            self.assertTrue(execution.ok, execution)
            self.assertFalse((workspace / "game").exists())
            self.assertEqual((), runtime.current.context.modified_directories)

    def test_runtime_refreshes_compensated_directory_identities_for_retry(self) -> None:
        """撤销中途失败但补偿完整后，Runtime 应刷新账本并允许再次撤销。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SessionStore(root / "state.db")
            store.initialize(workspace)
            record = store.create(
                "directory-undo-retry", workspace, "openai", "test-model"
            )

            def factory(session_record, memory, _options):  # type: ignore[no-untyped-def]
                journal = ChangeJournal()
                registry = ToolRegistry(
                    ToolContext(
                        WorkspacePolicy(workspace),
                        CommandPolicy(workspace),
                        lambda *_args: True,
                        change_journal=journal,
                    )
                )
                provider = StructuredScriptedProvider(
                    [
                        ProviderResponse(
                            tool_calls=(
                                ToolCall(
                                    "mkdir",
                                    "create_directory",
                                    {"path": "game/src"},
                                ),
                            )
                        ),
                        ProviderResponse(
                            tool_calls=(
                                ToolCall(
                                    "finish",
                                    "finish",
                                    {"summary": "目录创建完成"},
                                ),
                            )
                        ),
                    ]
                )
                config = AppConfig(
                    workspace=workspace,
                    provider=ProviderConfig(
                        "openai",
                        "synthetic-key",
                        "https://example.test",
                        "test-model",
                    ),
                )
                return ActiveSession(
                    session_record,
                    memory,
                    SessionContext(
                        persisted_summary=memory.summary,
                        modified_files=memory.modified_files,
                        verification=memory.verification,
                    ),
                    config,
                    CodingAgent(
                        provider,
                        registry,
                        max_rounds=2,
                        plan_enabled=False,
                    ),
                    registry,
                    journal,
                )

            runtime = SessionRuntime(
                store,
                workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=factory,
                initial_session_id=record.id,
                workspace_confirmer=lambda _preview: True,
            )
            result = runtime.run_task("创建 game/src 空目录")
            self.assertTrue(result.ok, result.summary)
            original = runtime.current.journal.latest()
            assert original is not None
            original_src = next(
                change.after
                for change in original.directory_changes
                if change.path == "game/src"
            )
            assert original_src is not None
            prior_evidence = object()
            runtime.current = replace(
                runtime.current,
                context=replace(
                    runtime.current.context,
                    verification="通过",
                    verification_evidence=prior_evidence,  # type: ignore[arg-type]
                    verification_required=False,
                ),
                memory=replace(runtime.current.memory, verification="通过"),
            )

            runtime.prepare_undo()
            probe = _DirectoryBinding.open(workspace, workspace)
            binding_type = type(probe)
            probe.close()
            original_remove = binding_type.remove_empty_directory

            def fail_outer(binding, name, expected):  # type: ignore[no-untyped-def]
                if name == "game":
                    raise OSError("simulated outer removal failure")
                return original_remove(binding, name, expected)

            with patch.object(
                binding_type,
                "remove_empty_directory",
                new=fail_outer,
            ):
                first = runtime.undo_latest()

            refreshed = runtime.current.journal.latest()
            assert refreshed is not None
            refreshed_src = next(
                change.after
                for change in refreshed.directory_changes
                if change.path == "game/src"
            )
            assert refreshed_src is not None
            self.assertFalse(first.ok)
            self.assertEqual((), first.compensation_failed)
            self.assertNotEqual(original_src.identity, refreshed_src.identity)
            self.assertEqual("待验证", runtime.current.context.verification)
            self.assertIsNone(runtime.current.context.verification_evidence)
            self.assertTrue(runtime.current.context.verification_required)
            self.assertEqual("待验证", runtime.current.memory.verification)

            runtime.prepare_undo()
            retry = runtime.undo_latest()
            runtime.close()

            self.assertTrue(retry.ok, retry)
            self.assertFalse((workspace / "game").exists())

    def test_create_directory_then_apply_patch_uses_the_same_directory_ledger(self) -> None:
        """apply_patch 继续要求父目录先存在，并与目录账本形成一个撤销集合。"""
        journal = ChangeJournal()
        journal.begin_task((), "未运行")
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                RecordingApprover(True, True),
                change_journal=journal,
            )
        )
        self.assertTrue(
            registry.execute("create_directory", {"path": "game"}).ok
        )
        patch_text = (
            "--- /dev/null\n"
            "+++ b/game/main.py\n"
            "@@ -0,0 +1 @@\n"
            "+value = 1\n"
        )

        result = registry.execute("apply_patch", {"patch": patch_text})
        effects = journal.active_effects()
        change_set = journal.seal_task(
            effects.paths,
            "待验证",
            effects.directory_paths,
        )

        self.assertTrue(result.ok, result.output)
        assert change_set is not None
        self.assertTrue(registry.undo_change_set(change_set).ok)
        self.assertEqual([], list(self.workspace.iterdir()))


class DirectoryProtocolFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    async def test_native_async_entry_creates_directory(self) -> None:
        """异步原生工具入口必须走同一可信目录账本。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            journal = ChangeJournal()
            journal.begin_task((), "未运行")
            registry = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(workspace),
                    CommandPolicy(workspace),
                    RecordingApprover(True),
                    change_journal=journal,
                )
            )
            provider = StructuredScriptedProvider(
                [
                    ProviderResponse(
                        tool_calls=(
                            ToolCall("mkdir", "create_directory", {"path": "game"}),
                        )
                    ),
                    ProviderResponse(
                        tool_calls=(
                            ToolCall("finish", "finish", {"summary": "done"}),
                        )
                    ),
                ]
            )
            agent = CodingAgent(
                provider, registry, max_rounds=2, plan_enabled=False
            )

            turn = await agent.run_with_context_async(
                "创建 game", SessionContext()
            )

            self.assertTrue(turn.result.ok, turn.result.summary)
            self.assertEqual(("game",), turn.result.modified_directories)

    async def test_legacy_json_protocol_can_discover_and_create_directory(self) -> None:
        """legacy 协议提示与解析路径同样公开并执行 create_directory。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            journal = ChangeJournal()
            journal.begin_task((), "未运行")
            registry = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(workspace),
                    CommandPolicy(workspace),
                    RecordingApprover(True),
                    change_journal=journal,
                )
            )
            provider = ScriptedProvider(
                [
                    '{"tool":"create_directory","arguments":{"path":"game"},'
                    '"reason":"创建任务目录"}',
                    '{"tool":"finish","arguments":{"summary":"done"},'
                    '"reason":"提交结果"}',
                ]
            )
            agent = CodingAgent(
                provider,
                registry,
                max_rounds=2,
                plan_enabled=False,
                tool_protocol="legacy_json",
            )

            turn = await agent.run_with_context_async(
                "创建 game", SessionContext()
            )

            self.assertTrue(turn.result.ok, turn.result.summary)
            self.assertEqual(("game",), turn.result.modified_directories)

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_binding_directory_identity_rejects_link(self) -> None:
        """目录身份读取不得跟随符号链接或 Windows reparse point。"""
        real = self.workspace / "real"
        real.mkdir()
        link = self.workspace / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"当前账户不能创建目录链接：{exc}")

        binding = _DirectoryBinding.open(self.workspace, self.workspace)
        try:
            with self.assertRaises(OSError):
                binding.directory_identity("link")
        finally:
            binding.close()


if __name__ == "__main__":
    unittest.main()
