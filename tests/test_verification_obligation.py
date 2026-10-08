"""历史验证义务与本轮交付门禁的恢复回归。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    ProviderResponse,
    SessionMemory,
    ToolCall,
)
from tricoder.core.validation import CommandCheckRecord
from tricoder.policy import WorkspacePolicy
from tricoder.session.runtime import (
    RuntimeOptions,
    SessionRuntime,
    _check_target_covers_path,
    _normalized_check_target,
)
from tricoder.session.store import SessionError, SessionStore


class _QueueProvider:
    """只返回合成响应；队列耗尽即暴露隐藏的额外模型请求。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        if not self._responses:
            raise AssertionError("Provider 响应队列已耗尽")
        return self._responses.pop(0)


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(ToolCall(call_id, name, arguments),),
        finish_reason="tool_calls",
    )


def _finish(call_id: str, summary: str = "已完成只读回顾") -> ProviderResponse:
    return _call(
        call_id,
        "finish",
        {"summary": summary, "outcome": "completed"},
    )


class VerificationObligationRecoveryTests(unittest.TestCase):
    """R0：实际恢复入口不得把历史展示状态变成本轮修改义务。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self.database = (self.root / "state" / "sessions.db").resolve()
        store = SessionStore(self.database, id_factory=lambda: "verification-scope")
        store.initialize(self.workspace)
        self.record = store.create(
            "verification-scope",
            self.workspace,
            "openai",
            "synthetic-model",
        )

    def _config(self) -> AppConfig:
        return AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                "openai",
                "synthetic-test-key",
                "https://example.test/v1",
                "synthetic-model",
            ),
            audit_dir=(self.root / "state" / "audit").resolve(),
            plan_enabled=False,
            memory=MemoryConfig(compaction="off", persistence="off"),
        )

    def _runtime(self, responses: list[ProviderResponse]) -> SessionRuntime:
        provider = _QueueProvider(responses)
        runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=lambda **_kwargs: self._config(),
            provider_factory=lambda _config, _timeout: provider,
            initial_session_id=self.record.id,
            approver=lambda _action, _detail: True,
            workspace_confirmer=lambda _preview: True,
        )
        return runtime

    def _restart_and_review(self) -> tuple[SessionRuntime, object]:
        restarted = self._runtime([_finish("review-finish")])
        result = restarted.run_task("回顾一下之前做了什么，不修改任何文件")
        return restarted, result

    def test_failed_check_history_does_not_create_review_task_obligation(self) -> None:
        first = self._runtime([
            _call(
                "failed-check",
                "run_command",
                {"command": "python -m compileall -q broken.py"},
            ),
            _finish("first-finish", "已报告语法检查失败"),
        ])
        try:
            initial = first.run_task("只读检查 broken.py 并报告结果")
            self.assertTrue(initial.ok, initial.summary)
            self.assertEqual("failed", first.current.memory.verification)
        finally:
            self.assertTrue(first.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual("failed", restarted.current.memory.verification)
            self.assertFalse(restarted.current.context.verification_required)
        finally:
            restarted.close()

    def test_passed_check_history_does_not_create_review_task_obligation(self) -> None:
        first = self._runtime([
            _call(
                "passed-check",
                "run_command",
                {"command": "python -m compileall -q app.py"},
            ),
            _finish("first-finish", "只读检查已完成"),
        ])
        try:
            initial = first.run_task("只读检查 app.py")
            self.assertTrue(initial.ok, initial.summary)
            self.assertEqual("passed", first.current.memory.verification)
        finally:
            self.assertTrue(first.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertFalse(restarted.current.context.verification_required)
            self.assertIsNone(restarted.current.context.verification_evidence)
        finally:
            restarted.close()

    def test_pending_modified_history_survives_successful_read_only_review(self) -> None:
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("first-finish", "文件已修改"),
        ])
        try:
            changed = first.run_task("把 app.py 中的值改为 2")
            self.assertFalse(changed.ok)
            self.assertEqual(("app.py",), first.current.memory.modified_files)
            self.assertEqual("pending", first.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), first.current.memory.pending_verification_paths
            )
        finally:
            self.assertTrue(first.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual(("app.py",), restarted.current.memory.modified_files)
            self.assertEqual("待验证", restarted.current.memory.verification)
            self.assertEqual(
                "pending", restarted.current.memory.verification_obligation
            )
            self.assertEqual(
                ("app.py",), restarted.current.memory.pending_verification_paths
            )
            self.assertFalse(restarted.current.context.verification_required)
        finally:
            restarted.close()

    def test_current_edit_still_requires_verification_and_net_zero_is_not_review(self) -> None:
        runtime = self._runtime([
            _call(
                "edit-forward",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _call(
                "edit-back",
                "edit_file",
                {"path": "app.py", "old_text": "x = 2", "new_text": "x = 1"},
            ),
            _finish("finish-net-zero", "已恢复原内容"),
        ])
        try:
            result = runtime.run_task("修改后再恢复 app.py")
            self.assertFalse(result.ok)
            self.assertEqual("pending", runtime.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), runtime.current.memory.pending_verification_paths
            )
        finally:
            runtime.close()

    def test_same_process_review_does_not_reuse_previous_task_gate(self) -> None:
        runtime = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
            _finish("finish-review", "已回顾未验证修改"),
        ])
        try:
            self.assertFalse(runtime.run_task("修改 app.py").ok)
            reviewed = runtime.run_task("回顾刚才做了什么，不修改文件")
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertFalse(reviewed.current_verification_required)
            self.assertEqual("pending", reviewed.verification_obligation)
            self.assertEqual(("app.py",), reviewed.pending_verification_paths)
        finally:
            runtime.close()

    def test_current_directory_creation_establishes_current_and_historical_obligation(self) -> None:
        runtime = self._runtime([
            _call("create-dir", "create_directory", {"path": "generated"}),
            _finish("finish-dir", "目录已创建"),
        ])
        try:
            result = runtime.run_task("创建 generated 目录")
            self.assertFalse(result.ok)
            self.assertTrue(result.current_verification_required)
            self.assertEqual("pending", result.verification_obligation)
            self.assertEqual(("generated",), result.pending_verification_paths)
        finally:
            runtime.close()

    def test_relevant_host_check_can_clear_only_covered_pending_path(self) -> None:
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-app",
                "run_command",
                {"command": "python -m compileall -q app.py"},
            ),
            _finish("finish-check", "已检查历史修改"),
        ])
        try:
            result = checked.run_task("检查 app.py 的历史修改")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("none", checked.current.memory.verification_obligation)
            self.assertEqual((), checked.current.memory.pending_verification_paths)
        finally:
            checked.close()

    def test_unrelated_host_check_does_not_clear_pending_path(self) -> None:
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-other",
                "run_command",
                {"command": "python -m compileall -q broken.py"},
            ),
            _finish("finish-check", "已报告无关检查失败"),
        ])
        try:
            result = checked.run_task("只读检查另一个文件")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("pending", checked.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), checked.current.memory.pending_verification_paths
            )
        finally:
            checked.close()

    def test_subdirectory_dot_check_does_not_clear_workspace_root_obligation(self) -> None:
        """检查目标必须相对命令 cwd 解析，不能把子目录的点号当成根目录。"""

        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "fine.py").write_text("x = 0\n", encoding="utf-8")
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-subdirectory",
                "run_command",
                {"command": "python -m compileall -q .", "cwd": "sub"},
            ),
            _finish("finish-check", "已检查 sub 目录"),
        ])
        try:
            result = checked.run_task("检查 sub 目录，不修改文件")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("sub", result.task_validation.records[0].cwd)
            self.assertEqual((".",), result.task_validation.records[0].targets)
            self.assertEqual("pending", checked.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), checked.current.memory.pending_verification_paths
            )
        finally:
            self.assertTrue(checked.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual("pending", restarted.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), restarted.current.memory.pending_verification_paths
            )
        finally:
            restarted.close()

    def test_subdirectory_check_only_clears_matching_pending_path(self) -> None:
        """根目录与子目录义务并存时，sub/. 只能移除子目录内路径。"""

        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "fine.py").write_text("x = 0\n", encoding="utf-8")
        first = self._runtime([
            _call(
                "edit-root",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _call(
                "edit-sub",
                "edit_file",
                {"path": "sub/fine.py", "old_text": "x = 0", "new_text": "x = 3"},
            ),
            _finish("finish-edit", "两个文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改根目录与子目录文件").ok)
            self.assertEqual(
                ("app.py", "sub/fine.py"),
                first.current.memory.pending_verification_paths,
            )
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-subdirectory",
                "run_command",
                {"command": "python -m compileall -q .", "cwd": "sub"},
            ),
            _finish("finish-check", "已检查子目录"),
        ])
        try:
            result = checked.run_task("只检查 sub 目录")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("pending", result.verification_obligation)
            self.assertEqual(("app.py",), result.pending_verification_paths)
        finally:
            checked.close()

    def test_zero_test_exit_zero_does_not_clear_obligation_after_restart(self) -> None:
        """退出码为零但明确运行零测试时，只保留诊断而不解除义务。"""

        first = self._runtime([
            _call(
                "break-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "def broken(:"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "zero-tests",
                "run_command",
                {"command": "python -m unittest discover -v"},
            ),
            _finish("finish-check", "没有发现测试"),
        ])
        try:
            result = checked.run_task("运行测试并报告，不修改文件")
            self.assertTrue(result.ok, result.summary)
            record = result.task_validation.records[0]
            self.assertEqual(0, record.returncode)
            self.assertIn("zero_tests_reported", record.diagnostics)
            self.assertEqual("pending", checked.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), checked.current.memory.pending_verification_paths
            )
        finally:
            self.assertTrue(checked.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual("pending", restarted.current.memory.verification_obligation)
            self.assertEqual(
                ("app.py",), restarted.current.memory.pending_verification_paths
            )
        finally:
            restarted.close()

    def test_zero_tests_alone_do_not_verify_current_modification(self) -> None:
        """零测试仍是成功执行，但不能成为本轮写入的唯一验证证据。"""

        runtime = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _call(
                "zero-tests",
                "run_command",
                {"command": "python -m unittest discover -v"},
            ),
            _finish("finish-edit", "修改和测试已执行"),
        ])
        try:
            result = runtime.run_task("修改 app.py 并运行测试")
            self.assertFalse(result.ok)
            self.assertTrue(result.current_verification_required)
            self.assertEqual(0, result.task_validation.records[0].returncode)
            self.assertIn(
                "zero_tests_reported", result.task_validation.records[0].diagnostics
            )
            self.assertEqual("pending", result.verification_obligation)
            self.assertEqual(("app.py",), result.pending_verification_paths)
        finally:
            runtime.close()

    def test_filtered_root_check_does_not_claim_complete_scope(self) -> None:
        """带排除条件的点号目标范围不完整，不能解除历史路径义务。"""

        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "filtered-check",
                "run_command",
                {"command": "python -m compileall -q -x broken.py ."},
            ),
            _finish("finish-check", "已运行带排除条件的检查"),
        ])
        try:
            result = checked.run_task("运行带筛选条件的检查")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual(0, result.task_validation.records[0].returncode)
            self.assertIn("scope_filtered", result.task_validation.records[0].diagnostics)
            self.assertEqual("pending", result.verification_obligation)
            self.assertEqual(("app.py",), result.pending_verification_paths)
        finally:
            checked.close()

    def test_compileall_depth_limit_keeps_deeper_obligation_after_restart(self) -> None:
        """显式 -r 深度会缩小点号范围，不能清除更深路径和历史未知来源。"""

        (self.workspace / "broken.py").unlink()
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "broken.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        store = SessionStore(self.database)
        memory = store.load_memory(self.record.id)
        store.save_memory(
            self.record.id,
            SessionMemory(
                summary=memory.summary,
                requirements_summary=memory.requirements_summary,
                last_task_summary=memory.last_task_summary,
                modified_files=memory.modified_files,
                verification="failed",
                permission_level=memory.permission_level,
                verification_obligation="legacy_unknown",
            ),
        )
        first = self._runtime([
            _call(
                "break-sub",
                "edit_file",
                {
                    "path": "sub/broken.py",
                    "old_text": "x = 1",
                    "new_text": "def broken(:",
                },
            ),
            _finish("finish-edit", "子目录文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 sub/broken.py").ok)
            self.assertEqual(
                "legacy_unknown", first.current.memory.verification_obligation
            )
            self.assertEqual(
                ("sub/broken.py",), first.current.memory.pending_verification_paths
            )
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "depth-limited-check",
                "run_command",
                {"command": "python -m compileall -q -r 0 ."},
            ),
            _finish("finish-check", "已报告深度受限检查"),
        ])
        try:
            result = checked.run_task("运行深度受限检查并报告")
            self.assertTrue(result.ok, result.summary)
            record = result.task_validation.records[0]
            self.assertEqual(0, record.returncode)
            self.assertIn("scope_filtered", record.diagnostics)
            self.assertEqual("legacy_unknown", result.verification_obligation)
            self.assertEqual(("sub/broken.py",), result.pending_verification_paths)
        finally:
            self.assertTrue(checked.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual(
                "legacy_unknown", restarted.current.memory.verification_obligation
            )
            self.assertEqual(
                ("sub/broken.py",), restarted.current.memory.pending_verification_paths
            )
        finally:
            restarted.close()

    def test_compileall_depth_limit_cannot_verify_current_subdirectory_write(self) -> None:
        """本轮写入子目录时，只有 -r 0 的根检查不能签发通过能力。"""

        (self.workspace / "broken.py").unlink()
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "app.py").write_text("x = 1\n", encoding="utf-8")
        runtime = self._runtime([
            _call(
                "edit-sub",
                "edit_file",
                {"path": "sub/app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _call(
                "depth-limited-check",
                "run_command",
                {"command": "python -m compileall -q -r 0 ."},
            ),
            _finish("finish-edit", "子目录修改和检查已执行"),
        ])
        try:
            result = runtime.run_task("修改子目录文件并运行深度受限检查")
            self.assertFalse(result.ok)
            self.assertTrue(result.current_verification_required)
            record = result.task_validation.records[0]
            self.assertEqual(0, record.returncode)
            self.assertIn("scope_filtered", record.diagnostics)
            self.assertEqual("pending", result.verification_obligation)
            self.assertEqual(("sub/app.py",), result.pending_verification_paths)
        finally:
            runtime.close()

    def test_effective_check_after_zero_tests_can_verify_current_modification(self) -> None:
        """零测试不污染后续有效检查；最终能力只来自后者。"""

        runtime = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _call(
                "zero-tests",
                "run_command",
                {"command": "python -m unittest discover -v"},
            ),
            _call(
                "syntax-check",
                "run_command",
                {"command": "python -m compileall -q app.py"},
            ),
            _finish("finish-edit", "修改已完成并检查"),
        ])
        try:
            result = runtime.run_task("修改 app.py 并完成有效检查")
            self.assertTrue(result.ok, result.summary)
            # required 记录本轮曾产生写入义务；成功由受信 evidence 满足，而非抹掉事实。
            self.assertTrue(result.current_verification_required)
            self.assertEqual("none", result.verification_obligation)
            self.assertEqual((), result.pending_verification_paths)
        finally:
            runtime.close()

    def test_legacy_unknown_coexists_with_paths_and_survives_local_check(self) -> None:
        """来源不明义务与已知路径可共存；局部成功只能清除已覆盖路径。"""

        store = SessionStore(self.database)
        store.save_memory(
            self.record.id,
            SessionMemory(
                verification="failed",
                verification_obligation="legacy_unknown",
            ),
        )
        first = self._runtime([
            _call(
                "edit-app",
                "edit_file",
                {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
            ),
            _finish("finish-edit", "文件已修改"),
        ])
        try:
            self.assertFalse(first.run_task("修改 app.py").ok)
            self.assertEqual(
                "legacy_unknown", first.current.memory.verification_obligation
            )
            self.assertEqual(
                ("app.py",), first.current.memory.pending_verification_paths
            )
        finally:
            self.assertTrue(first.close())

        checked = self._runtime([
            _call(
                "check-app",
                "run_command",
                {"command": "python -m compileall -q app.py"},
            ),
            _finish("finish-check", "已检查已知路径"),
        ])
        try:
            result = checked.run_task("只检查 app.py，不修改文件")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual(
                "legacy_unknown", checked.current.memory.verification_obligation
            )
            self.assertEqual((), checked.current.memory.pending_verification_paths)
        finally:
            self.assertTrue(checked.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual(
                "legacy_unknown", restarted.current.memory.verification_obligation
            )
            self.assertEqual((), restarted.current.memory.pending_verification_paths)
        finally:
            restarted.close()

    def test_external_workspace_change_preserves_legacy_unknown_and_paths(self) -> None:
        """工作区门禁发现外部变化时，也不能用 pending 覆盖历史未知来源。"""

        store = SessionStore(self.database)
        store.save_memory(
            self.record.id,
            SessionMemory(
                verification="failed",
                verification_obligation="legacy_unknown",
            ),
        )
        runtime = self._runtime([
            _finish("finish-baseline", "已初始化工作区基线"),
            _finish("finish-review", "已确认外部变化"),
        ])
        try:
            baseline = runtime.run_task("初始化后只读回顾")
            self.assertTrue(baseline.ok, baseline.summary)
            (self.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
            result = runtime.run_task("只读说明工作区变化")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("legacy_unknown", result.verification_obligation)
            self.assertEqual(("app.py",), result.pending_verification_paths)
        finally:
            self.assertTrue(runtime.close())

        restarted, reviewed = self._restart_and_review()
        try:
            self.assertTrue(reviewed.ok, reviewed.summary)
            self.assertEqual(
                "legacy_unknown", restarted.current.memory.verification_obligation
            )
            self.assertEqual(
                ("app.py",), restarted.current.memory.pending_verification_paths
            )
        finally:
            restarted.close()

    def test_effective_workspace_root_check_can_clear_legacy_unknown(self) -> None:
        """只有当前完整根范围检查，才可以解除来源未知义务。"""

        (self.workspace / "broken.py").unlink()
        store = SessionStore(self.database)
        store.save_memory(
            self.record.id,
            SessionMemory(
                verification="failed",
                verification_obligation="legacy_unknown",
            ),
        )
        runtime = self._runtime([
            _call(
                "root-check",
                "run_command",
                {"command": "python -m compileall -q ."},
            ),
            _finish("finish-check", "已检查工作区文件"),
        ])
        try:
            result = runtime.run_task("检查工作区已知源码")
            self.assertTrue(result.ok, result.summary)
            self.assertEqual("none", result.verification_obligation)
            self.assertEqual((), result.pending_verification_paths)
        finally:
            runtime.close()

    def test_legacy_unknown_survives_review_without_becoming_current_gate(self) -> None:
        store = SessionStore(self.database)
        memory = store.load_memory(self.record.id)
        store.save_memory(
            self.record.id,
            SessionMemory(
                summary=memory.summary,
                requirements_summary=memory.requirements_summary,
                last_task_summary=memory.last_task_summary,
                modified_files=memory.modified_files,
                verification="failed",
                permission_level=memory.permission_level,
                verification_obligation="legacy_unknown",
            ),
        )

        restarted, result = self._restart_and_review()
        try:
            self.assertTrue(result.ok, result.summary)
            self.assertFalse(result.current_verification_required)
            self.assertEqual("legacy_unknown", result.verification_obligation)
            self.assertEqual(
                "legacy_unknown", restarted.current.memory.verification_obligation
            )
        finally:
            restarted.close()


class VerificationTargetNormalizationTests(unittest.TestCase):
    """R1：检查 cwd 与 target 必须先进入统一工作区坐标。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "app.py").write_text("x = 2\n", encoding="utf-8")
        (self.workspace / "submarine").mkdir()
        (self.workspace / "submarine" / "app.py").write_text(
            "x = 3\n", encoding="utf-8"
        )
        self.policy = WorkspacePolicy(self.workspace)

    @staticmethod
    def _record(cwd: str) -> CommandCheckRecord:
        return CommandCheckRecord(
            task_id="task",
            check_id=f"check-{cwd}",
            argv=("python", "-m", "compileall", "."),
            cwd=cwd,
            kind="syntax",
            returncode=0,
            output_summary="",
            execution_complete=True,
            workspace_stable=True,
            targets=(".",),
            snapshot_id="snapshot",
        )

    def test_subdirectory_dot_and_segment_boundary(self) -> None:
        target = _normalized_check_target(self._record("sub"), ".", self.policy)
        self.assertEqual("sub", target)
        self.assertTrue(_check_target_covers_path(target, "sub/app.py"))
        self.assertFalse(_check_target_covers_path(target, "submarine/app.py"))
        self.assertFalse(_check_target_covers_path(target, "app.py"))

    def test_relative_prefix_and_internal_parent_are_normalized(self) -> None:
        root_record = self._record(".")
        sub_record = self._record("sub")
        self.assertEqual(
            "app.py",
            _normalized_check_target(root_record, "./app.py", self.policy),
        )
        self.assertEqual(
            "app.py",
            _normalized_check_target(sub_record, "../app.py", self.policy),
        )
        if os.name == "nt":
            self.assertEqual(
                "sub",
                _normalized_check_target(root_record, "SUB", self.policy),
            )

    def test_escape_and_missing_targets_have_no_coverage_authority(self) -> None:
        record = self._record("sub")
        self.assertIsNone(
            _normalized_check_target(record, "../../outside.py", self.policy)
        )
        self.assertIsNone(
            _normalized_check_target(record, "missing.py", self.policy)
        )


class VerificationObligationStoreTests(unittest.TestCase):
    """R1：验证义务元数据使用追加迁移并严格校验。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        self.store = SessionStore(self.database, id_factory=lambda: "store-session")
        self.store.initialize(self.workspace)
        self.record = self.store.create(
            "store-session", self.workspace, "openai", "synthetic-model"
        )

    def test_new_session_defaults_and_pending_paths_roundtrip(self) -> None:
        created = self.store.load_memory(self.record.id)
        self.assertEqual("none", created.verification_obligation)
        self.assertEqual((), created.pending_verification_paths)

        pending = SessionMemory(
            modified_files=("src/app.py", "tests/test_app.py"),
            verification="待验证",
            verification_obligation="pending",
            pending_verification_paths=("src/app.py", "tests/test_app.py"),
        )
        self.store.save_memory(self.record.id, pending)

        self.assertEqual(pending, self.store.load_memory(self.record.id))

    def test_legacy_unknown_can_roundtrip_with_known_pending_paths(self) -> None:
        combined = SessionMemory(
            verification="待验证",
            verification_obligation="legacy_unknown",
            pending_verification_paths=("src/app.py",),
        )
        self.store.save_memory(self.record.id, combined)

        self.assertEqual(combined, self.store.load_memory(self.record.id))

    def test_none_with_paths_and_pending_without_paths_are_rejected(self) -> None:
        for memory in (
            SessionMemory(
                verification_obligation="none",
                pending_verification_paths=("src/app.py",),
            ),
            SessionMemory(verification_obligation="pending"),
        ):
            with self.subTest(obligation=memory.verification_obligation):
                with self.assertRaises(SessionError):
                    self.store.save_memory(self.record.id, memory)

    def test_old_schema_migrates_obligations_conservatively_and_idempotently(self) -> None:
        legacy_database = (self.root / "legacy" / "sessions.db").resolve()
        legacy_database.parent.mkdir(parents=True)
        connection = sqlite3.connect(legacy_database)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE session_memory (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    requirements_summary TEXT NOT NULL DEFAULT '',
                    last_task_summary TEXT NOT NULL DEFAULT '',
                    modified_files_json TEXT NOT NULL DEFAULT '[]',
                    verification TEXT NOT NULL DEFAULT '未运行',
                    permission TEXT NOT NULL DEFAULT 'strict',
                    unknown_effects INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                """
            )
            cases = (
                ("empty", "未运行", "[]", 0),
                ("passed", "passed", "[]", 0),
                ("failed", "failed", "[]", 0),
                ("pending-path", "待验证", '["src/app.py"]', 0),
                ("unknown", "未运行", "[]", 1),
            )
            for session_id, verification, files_json, unknown in cases:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        session_id,
                        str(self.workspace),
                        "openai",
                        "synthetic-model",
                        "2026-10-07T00:00:00+00:00",
                        "2026-10-07T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO session_memory (
                        session_id, modified_files_json, verification, unknown_effects
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session_id, files_json, verification, unknown),
                )
            connection.commit()
        finally:
            connection.close()

        migrated = SessionStore(legacy_database)
        migrated.initialize(self.workspace)
        migrated.initialize(self.workspace)

        self.assertEqual("none", migrated.load_memory("empty").verification_obligation)
        self.assertEqual(
            "legacy_unknown",
            migrated.load_memory("passed").verification_obligation,
        )
        self.assertEqual(
            "legacy_unknown",
            migrated.load_memory("failed").verification_obligation,
        )
        pending = migrated.load_memory("pending-path")
        self.assertEqual("pending", pending.verification_obligation)
        self.assertEqual(("src/app.py",), pending.pending_verification_paths)
        unknown = migrated.load_memory("unknown")
        self.assertEqual("legacy_unknown", unknown.verification_obligation)
        self.assertTrue(unknown.unknown_effects)

    def test_corrupted_or_unsafe_obligation_metadata_is_rejected_atomically(self) -> None:
        original = self.store.load_memory(self.record.id)
        with self.assertRaises(SessionError):
            self.store.save_memory(
                self.record.id,
                SessionMemory(
                    verification_obligation="pending",
                    pending_verification_paths=("../outside.py",),
                ),
            )
        self.assertEqual(original, self.store.load_memory(self.record.id))

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE session_memory
                SET verification_obligation = ?, pending_verification_paths_json = ?
                WHERE session_id = ?
                """,
                ("invalid", "{not-json", self.record.id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SessionError):
            self.store.load_memory(self.record.id)


if __name__ == "__main__":
    unittest.main()
