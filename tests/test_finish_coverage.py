"""定向检查与分次检查覆盖：真实文件工具、命令和最终收尾。"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_finish_verification_recovery import RecordingProvider, response
from tricoder.agent import CodingAgent
from tricoder.engine.completion import has_relevant_check
from tricoder.engine.state import AgentRunState
from tricoder.engine.telemetry import NullObserver
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, SessionContext
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.session.runtime import RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.workspace.verification import VerificationScope, WorkspaceSnapshot


FINISH = ("finish", {"summary": "完成检查", "outcome": "completed"})


def create(path):
    return "create_file", {"path": path, "content": "value = 1\n", "create_parents": True}


def check(target, cwd="."):
    return "run_command", {"command": f"python -m compileall -q {target}", "cwd": cwd}


class FinishCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.tools = ToolRegistry(ToolContext(
            WorkspacePolicy(self.workspace), CommandPolicy(),
            approver=lambda *_: True, timeout=5,
        ))

    def run_actions(self, actions, observer=None):
        provider = RecordingProvider([
            response(str(index), tool, arguments)
            for index, (tool, arguments) in enumerate(actions)
        ])
        turn = CodingAgent(
            provider, self.tools, max_rounds=len(actions), plan_enabled=False, observer=observer,
            memory_config=MemoryConfig(compaction="off", persistence="off"),
        ).run_with_context("检查合成源码", SessionContext())
        self.feedback = [json.loads(m.content) for m in turn.context.messages
                         if m.kind == "verification_feedback"]
        # 恢复或拒绝不能导致工具结果重复、丢失。
        calls = [call.id for m in turn.context.messages for call in m.tool_calls]
        results = [m.tool_call_id for m in turn.context.messages if m.role == "tool"]
        self.assertCountEqual(calls, results)
        self.assertEqual(len(results), len(set(results)))
        return turn

    def test_new_parents_targeted_file_check(self):
        turn = self.run_actions([create("app/demo/main.py"), check("app/demo/main.py"), FINISH, FINISH])
        self.assertTrue(turn.result.ok, turn.result)
        self.assertEqual(("app", "app/demo"), turn.context.modified_directories)
        self.assertEqual("通过", turn.result.verification)
        self.assertEqual(3, turn.result.rounds)

    def test_explicit_directory_then_targeted_file_check(self):
        turn = self.run_actions([
            ("create_directory", {"path": "app/demo", "parents": True}),
            create("app/demo/main.py"), check("app/demo/main.py"), FINISH, FINISH,
        ])
        self.assertTrue(turn.result.ok, turn.result)
        self.assertIn("app/demo", turn.context.modified_directories)

    def test_existing_parents_control(self):
        (self.workspace / "app/demo").mkdir(parents=True)
        turn = self.run_actions([create("app/demo/main.py"), check("app/demo/main.py"), FINISH])
        self.assertTrue(turn.result.ok, turn.result)
        self.assertEqual((), turn.context.modified_directories)

    def test_separate_checks_union_and_partial_feedback(self):
        turn = self.run_actions([
            create("a.py"), create("b.py"), check("a.py"), check("b.py"), FINISH, FINISH,
        ])
        self.assertTrue(turn.result.ok, turn.result)
        syntax = [f for f in self.feedback if f["check_kind"] == "syntax"]
        self.assertEqual(["b.py"], syntax[0]["pending_paths"])
        self.assertEqual([], syntax[1]["pending_paths"])
        self.assertEqual("satisfied", syntax[1]["verification_obligation"])

    def test_one_combined_check_control(self):
        turn = self.run_actions([create("a.py"), create("b.py"), check("a.py b.py"), FINISH])
        self.assertTrue(turn.result.ok, turn.result)

    def test_partial_finish_recovers_once_and_unions_later_check(self):
        turn = self.run_actions([
            create("a.py"), create("b.py"), check("a.py"), FINISH, check("b.py"), FINISH,
        ])
        self.assertTrue(turn.result.ok, turn.result)
        recovery = [f for f in self.feedback if f.get("recovery_count") is not None]
        self.assertEqual(1, len(recovery))
        self.assertEqual(["b.py"], recovery[0]["pending_paths"])
        self.assertEqual(1, recovery[0]["recovery_count"])
        self.assertEqual(6, turn.result.rounds)

    def test_partial_check_stays_incomplete_and_only_reports_missing_file(self):
        turn = self.run_actions([
            create("app/a.py"), create("app/b.py"), check("app/a.py"), FINISH, FINISH,
        ])
        self.assertFalse(turn.result.ok)
        self.assertEqual(["app/b.py"], self.feedback[-1]["pending_paths"])

    def test_changes_between_checks_do_not_union_old_snapshot(self):
        turn = self.run_actions([
            create("a.py"), check("a.py"), create("b.py"), check("b.py"), FINISH,
        ])
        self.assertFalse(turn.result.ok)
        self.assertEqual(["a.py"], self.feedback[-1]["pending_paths"])

    def test_different_cwd_targets_union(self):
        turn = self.run_actions([
            create("left/main.py"), create("right/main.py"),
            check("./main.py", "left"), check("main.py", "right"), FINISH, FINISH,
        ])
        self.assertTrue(turn.result.ok, turn.result)
        syntax = [f for f in self.feedback if f["check_kind"] == "syntax"]
        self.assertEqual(["right/main.py"], syntax[0]["pending_paths"])

    def test_subdirectory_dot_does_not_cover_sibling(self):
        turn = self.run_actions([
            create("left/main.py"), create("right/main.py"), check(".", "left"), FINISH, FINISH,
        ])
        self.assertFalse(turn.result.ok)
        self.assertEqual(["right/main.py"], self.feedback[-1]["pending_paths"])

    def test_directory_only_without_check_is_not_silently_completed(self):
        turn = self.run_actions([("create_directory", {"path": "empty"}), FINISH, FINISH])
        self.assertFalse(turn.result.ok)
        self.assertEqual(("empty",), turn.context.modified_directories)

    def test_unrelated_empty_directory_keeps_its_verification_target(self):
        turn = self.run_actions([
            ("create_directory", {"path": "empty"}), create("a.py"), check("a.py"), FINISH, FINISH,
        ])
        self.assertFalse(turn.result.ok)
        self.assertEqual(["empty"], self.feedback[-1]["pending_paths"])

    def test_confirmed_parent_replacement_still_invalidates_final_snapshot(self):
        workspace = self.workspace
        backup = tempfile.TemporaryDirectory()
        self.addCleanup(backup.cleanup)
        old_parent = Path(backup.name) / "old-app"

        class ReplaceParent(NullObserver):
            def on_action(self, action):
                if action.tool == "finish":
                    # 保留真实文件对象，仅更换父目录身份；不是源码检查目标的目录仍受保护。
                    (workspace / "app").rename(old_parent)
                    (workspace / "app").mkdir()
                    (old_parent / "main.py").rename(workspace / "app/main.py")

        turn = self.run_actions(
            [create("app/main.py"), check("app/main.py"), FINISH], ReplaceParent(),
        )
        self.assertFalse(turn.result.ok)
        self.assertEqual("stale", turn.result.task_validation.status)
        self.assertEqual(("app",), turn.context.modified_directories)

    def test_filtered_second_check_does_not_complete_union(self):
        turn = self.run_actions([
            create("a.py"), create("b.py"), check("a.py"),
            ("run_command", {"command": "python -m compileall -q -r 0 b.py"}), FINISH, FINISH,
        ])
        self.assertFalse(turn.result.ok)
        self.assertIn("scope_filtered", turn.result.task_validation.records[-1].diagnostics)
        self.assertEqual(["b.py"], self.feedback[-1]["pending_paths"])

    def test_runtime_accepts_nested_file_check_and_following_readonly_task(self):
        """真实宿主收尾可交付，不把跨任务目录义务重新混入本轮门禁。"""
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            store = SessionStore(state_root / "sessions.db")
            store.initialize(self.workspace)
            record = store.create("coverage", self.workspace, "openai", "offline")
            config = AppConfig(
                workspace=self.workspace,
                provider=ProviderConfig("openai", "offline-placeholder", "https://example.invalid/v1", "offline"),
                audit_dir=state_root / "audit", plan_enabled=False,
                memory=MemoryConfig(compaction="off", persistence="off"),
            )
            actions = [create("app/demo/main.py"), check("app/demo/main.py"), FINISH]
            provider = RecordingProvider([
                response(str(index), tool, arguments)
                for index, (tool, arguments) in enumerate(actions)
            ])
            runtime = SessionRuntime(
                store, self.workspace, options=RuntimeOptions(environ={}),
                initial_session_id=record.id, config_loader=lambda **_: config,
                provider_factory=lambda *_: provider, approver=lambda *_: True,
                workspace_confirmer=lambda _: True,
            )
            try:
                result = runtime.run_task("创建并定向检查文件")
                self.assertTrue(result.ok, result)
                self.assertEqual(("app", "app/demo"), result.modified_directories)
                self.assertNotIn("app/demo/main.py", result.pending_verification_paths)
                self.assertEqual((), result.pending_verification_paths)
                provider.responses.append(response("review", *FINISH))
                result = runtime.run_task("只读回顾")
                self.assertTrue(result.ok, result)
                self.assertEqual(4, len(provider.histories))
                self.assertEqual("none", runtime.current.memory.verification_obligation)
            finally:
                runtime.close()
            provider.responses.append(response("restart-review", *FINISH))
            restored = SessionRuntime(
                SessionStore(state_root / "sessions.db"), self.workspace,
                options=RuntimeOptions(environ={}), initial_session_id=record.id,
                config_loader=lambda **_: config, provider_factory=lambda *_: provider,
                workspace_confirmer=lambda _: True,
            )
            try:
                self.assertEqual((), restored.current.memory.pending_verification_paths)
                self.assertTrue(restored.run_task("恢复后只读回顾").ok)
            finally:
                restored.close()


class CheckCoverageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.state = AgentRunState.start("system", "task", SessionContext())
        self.state.task_modified_files = ["a.py", "b.py"]
        self.scope = VerificationScope()
        self.scope.begin_task(self.state.current_task_id)
        self.snapshot = WorkspaceSnapshot(self.scope.token, "current", True)

    def record(self, target, **changes):
        record = self.scope.issue_command_check(
            argv=("python", "-m", "compileall", target), cwd=".", kind="syntax",
            returncode=0, output_summary="", execution_complete=True,
            workspace_stable=True, targets=(target,), snapshot_id="current",
        )
        return replace(record, **changes)

    def test_union_rejects_invalid_second_record(self):
        cases = [
            {"returncode": 1}, {"execution_complete": False}, {"workspace_stable": False},
            {"snapshot_id": "old"}, {"diagnostics": ("scope_filtered",)},
            {"kind": "tests", "diagnostics": ("zero_tests_reported",)},
            {"kind": "script"}, {"cwd": "elsewhere"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                state = AgentRunState.start("system", "task", SessionContext())
                state.task_modified_files = ["a.py", "b.py"]
                state.validation.observe(self.record("a.py"))
                state.validation.observe(self.record("b.py", **changes))
                self.assertFalse(has_relevant_check(state, self.snapshot))

    def test_invalidated_check_cannot_revive_when_digest_returns(self):
        self.state.validation.observe(self.record("a.py"))
        self.state.validation.invalidate()
        self.state.validation.observe(self.record("b.py"))
        self.assertFalse(has_relevant_check(self.state, self.snapshot))

    def test_incomplete_current_snapshot_cannot_union_checks(self):
        self.state.validation.observe(self.record("a.py"))
        self.state.validation.observe(self.record("b.py"))
        self.assertFalse(has_relevant_check(self.state, replace(self.snapshot, complete=False)))

    def test_old_success_cannot_override_latest_failed_same_check(self):
        self.state.validation.observe(self.record("a.py"))
        self.state.validation.observe(self.record("a.py", returncode=1))
        self.state.validation.observe(self.record("b.py"))
        self.assertFalse(has_relevant_check(self.state, self.snapshot))

    def test_other_task_record_is_rejected_before_union(self):
        with self.assertRaises(ValueError):
            self.state.validation.observe(self.record("a.py", task_id="another-task"))


if __name__ == "__main__":
    unittest.main()
