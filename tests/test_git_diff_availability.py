"""固定 Git helper 的能力缺失恢复不扩大普通命令/安全门禁。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.execution_state import EffectState, ErrorCode, FileEffects, RecoveryAction
from tricoder.models import MemoryConfig, tool_failure
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy
from tricoder.process.control import BoundedProcessResult, ProcessExecutionUncertain
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools import command
from tests.test_agent_termination import StrictQueueProvider
from tests.test_patch_feedback import response


class GitDiffAvailabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf8")
        self.registry = ToolRegistry(ToolContext(WorkspacePolicy(self.workspace), CommandPolicy(), lambda *_: True))

    def agent(self, provider, registry=None):
        return CodingAgent(provider, registry or self.registry, max_rounds=4, plan_enabled=False,
            memory_config=MemoryConfig(compaction="off", persistence="off"))

    def provider(self, first=None):
        return StrictQueueProvider([
            first or response("git_diff", {}),
            response("edit_file", {"path": "app.py", "old_text": "1", "new_text": "2"}),
            response("run_command", {"command": "python -m compileall -q app.py"}),
            response("finish", {"summary": "done"}),
        ])

    def test_confirmed_parent_root_returns_unavailable_without_diff_or_proof(self):
        with patch.object(command, "_git_toplevel", return_value=self.root), \
             patch.object(command, "run_bounded_process") as diff:
            result = self.registry.execute("git_diff", {})
        diff.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.EXECUTION_FAILED, result.error.code)
        self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
        self.assertEqual("git_workspace_unavailable", getattr(result.error, "detail_code", None))
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)
        self.assertEqual((), result.modified_paths)
        self.assertEqual("当前工作区不是独立仓库根，git_diff 不可用；未执行 diff。请使用当前工作区文件与已有变更记录继续验证，不要通过命令读取父仓库。", result.output)

    def test_same_agent_task_continues_edit_verify_finish_after_unavailable(self):
        provider = self.provider()
        with patch.object(command, "_git_toplevel", return_value=self.root):
            result = self.agent(provider).run("edit and verify")
        self.assertTrue(result.ok, result)
        self.assertEqual(4, len(provider.histories))
        self.assertEqual("通过", result.verification)
        feedback = [m.content for m in provider.histories[1] if m.role == "tool"]
        self.assertIn("git_workspace_unavailable", feedback[0])
        self.assertEqual("value = 2\n", (self.workspace / "app.py").read_text(encoding="utf8"))

    def test_ordinary_parent_git_command_still_stops_before_edit(self):
        provider = self.provider(response("run_command", {"command": "git status"}))
        with patch.object(command, "_git_toplevel", return_value=self.root), \
             patch.object(command, "run_bounded_process") as process:
            result = self.agent(provider).run("inspect")
        self.assertFalse(result.ok)
        process.assert_not_called()
        self.assertEqual(1, len(provider.histories))
        with patch.object(command, "_git_toplevel", return_value=self.root):
            denied = self.registry.execute("run_command", {"command": "git status"})
        self.assertEqual(ErrorCode.POLICY_DENIED, denied.error.code)
        self.assertEqual(RecoveryAction.STOP_TASK, denied.error.recovery)
        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))

    def test_cancellation_at_probe_return_is_not_reported_as_unavailable(self):
        token = CancellationToken()
        def probe(*_):
            token.cancel()
            return self.root
        with patch.object(command, "_git_toplevel", side_effect=probe), \
             patch.object(command, "run_bounded_process") as process:
            with self.assertRaises(CancellationError):
                self.registry.execute("git_diff", {}, cancellation=token)
        process.assert_not_called()
        token = CancellationToken()
        provider = self.provider()
        with patch.object(command, "_git_toplevel", side_effect=probe):
            result = self.agent(provider).run("inspect", cancellation=token)
        self.assertFalse(result.ok)
        self.assertEqual(1, len(provider.histories))
        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))

    def test_uncertain_probe_and_diff_failures_do_not_gain_availability_recovery(self):
        failures = (
            (BoundedProcessResult(None, "", "", timed_out=True), ErrorCode.TIMEOUT),
            (BoundedProcessResult(None, "", "", output_exceeded=True), ErrorCode.OUTPUT_LIMIT),
            (BoundedProcessResult(None, "", "", cleanup_failed=True), ErrorCode.CLEANUP_FAILED),
            (ProcessExecutionUncertain(cleanup_failed=True), ErrorCode.CLEANUP_FAILED),
            (ProcessExecutionUncertain(cleanup_failed=False), ErrorCode.RESULT_UNCERTAIN),
        )
        for failure, expected in failures:
            with self.subTest(expected=expected):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                # 探测不确定（旧探测返回 None），绝不能误归为已确认父仓库。
                with patch.object(command, "_git_toplevel", return_value=None), patch.object(command, "run_bounded_process", **kwargs):
                    result = self.registry.execute("git_diff", {})
                    provider = self.provider()
                    stopped = self.agent(provider).run("inspect")
                self.assertEqual(expected, result.error.code)
                self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
                self.assertIsNone(getattr(result.error, "detail_code", None))
                self.assertFalse(stopped.ok)
                self.assertEqual(1, len(provider.histories))
                self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))

    def test_approval_policy_and_unknown_still_prevent_later_write(self):
        denied_registry = ToolRegistry(ToolContext(WorkspacePolicy(self.workspace), CommandPolicy(), lambda *_: False))
        provider = self.provider(response("run_command", {"command": "python --version"}))
        self.assertFalse(self.agent(provider, denied_registry).run("inspect").ok)
        self.assertEqual(1, len(provider.histories))
        with patch.object(command, "_git_toplevel", return_value=self.workspace), \
             patch.object(self.registry.context.command_policy, "validate", side_effect=PolicyError("synthetic")):
            denied = self.registry.execute("git_diff", {})
        self.assertEqual(ErrorCode.POLICY_DENIED, denied.error.code)
        self.assertEqual(RecoveryAction.STOP_TASK, denied.error.recovery)
        uncertain = tool_failure(ErrorCode.EXECUTION_FAILED, "fixed", file_effects=FileEffects(EffectState.UNKNOWN))
        provider = self.provider(response("run_command", {"command": "python --version"}))
        # UNKNOWN 由受信任命令路径发布；只读 helper 自报的影响本就会被注册表丢弃。
        with patch.object(self.registry._handlers["run_command"], "run_with_cancellation", return_value=uncertain):
            self.assertFalse(self.agent(provider).run("inspect").ok)
        self.assertEqual(1, len(provider.histories))
        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))

    def test_equal_root_retains_diff_and_empty_diff_without_validation_claim(self):
        for stdout in (" app.py | 2 +-\n", ""):
            with self.subTest(stdout=stdout), patch.object(command, "_git_toplevel", return_value=self.workspace), \
                 patch.object(command, "run_bounded_process", return_value=BoundedProcessResult(0, stdout, "")) as diff:
                result = self.registry.execute("git_diff", {})
            self.assertTrue(result.ok)
            self.assertEqual(1, diff.call_count)
            self.assertIn("app.py" if stdout else "没有未提交变更", result.output)
            self.assertIsNone(result.verification_evidence)
        with patch.object(command, "_git_toplevel", return_value=None), \
             patch.object(command, "run_bounded_process", return_value=BoundedProcessResult(129, "", "not a git repository")):
            self.assertFalse(self.registry.execute("git_diff", {}).ok)
        with patch.object(command, "_git_toplevel", return_value=None), \
             patch.object(command, "run_bounded_process", side_effect=FileNotFoundError):
            missing = self.registry.execute("git_diff", {})
        self.assertFalse(missing.ok)
        self.assertNotIn("没有未提交变更", missing.output)

    @unittest.skipUnless(shutil.which("git"), "需要本地 Git")
    def test_real_worktree_git_file_is_supported(self):
        # 仅在临时合成仓库提交用于 worktree 的起点，不接触项目 Git 索引。
        repo = self.root / "repo"; repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
        git("init", "-q")
        (repo / "app.py").write_text("value = 1\n", encoding="utf8")
        git("add", "app.py")
        git("-c", "user.email=test@example.invalid", "-c", "user.name=test", "commit", "-qm", "fixture")
        worktree = self.root / "worktree"
        git("worktree", "add", "--detach", str(worktree))
        self.assertTrue((worktree / ".git").is_file())
        registry = ToolRegistry(ToolContext(WorkspacePolicy(worktree), CommandPolicy(), lambda *_: True))
        self.assertTrue(registry.execute("git_diff", {}).ok)
        (worktree / "app.py").write_text("value = 2\n", encoding="utf8")
        result = registry.execute("git_diff", {})
        self.assertTrue(result.ok, result.output)
        self.assertIn("app.py", result.output)


if __name__ == "__main__":
    unittest.main()
