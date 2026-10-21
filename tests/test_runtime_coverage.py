"""Runtime 覆盖与结构目录：临时 SQLite 恢复及历史来源对照。"""
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace

from test_finish_coverage import create, check, FINISH
from test_finish_verification_recovery import RecordingProvider, response
from tricoder.models import AppConfig, ProviderConfig, MemoryConfig, SessionMemory, RunResult
from tricoder.core.validation import TaskValidationReport
from tricoder.policy import WorkspacePolicy
from tricoder.session.store import SessionStore
from tricoder.session.runtime import SessionRuntime, RuntimeOptions, _merge_verification_obligation
from tricoder.workspace.verification import VerificationScope
from tricoder.execution_state import FileEffects, EffectState


class RuntimeCoverageTests(unittest.TestCase):
    def test_stale_identity_and_unresolved_survive_runtime_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.py").write_text("x=1", encoding="utf8")
            scope = VerificationScope()
            scope.begin_task("task-1")
            good = scope.issue_command_check(argv=("python", "-m", "compileall", "a.py"),
                cwd=".", kind="syntax", returncode=0, output_summary="", execution_complete=True,
                workspace_stable=True, targets=("a.py",), snapshot_id="current")
            historical = replace(good, check_id="old", argv=("python", "old"))
            reports = [
                (TaskValidationReport(records=(historical, good), status="observed", stale_check_ids=("old",)), "none"),
                (TaskValidationReport(records=(good,), status="observed", stale_check_ids=(good.check_id,)), "pending"),
                (TaskValidationReport(records=(good,), status="stale"), "pending"),
                (TaskValidationReport(records=(good,), status="failed", unresolved_check_ids=("evicted",)), "pending"),
                (TaskValidationReport(records=(replace(good, _authority=object()),), status="observed"), "pending"),
            ]
            for report, expected in reports:
                with self.subTest(report=report):
                    result = RunResult(ok=True, summary="", rounds=1, tool_calls=1, task_validation=report)
                    obligation, paths = _merge_verification_obligation(
                        SessionMemory(verification_obligation="pending", pending_verification_paths=("a.py",)),
                        FileEffects(EffectState.NONE), result, trusted_pass=True,
                        workspace_policy=WorkspacePolicy(root), verification_scope=scope,
                        current_snapshot_id="current")
                    self.assertEqual(expected, obligation)
                    self.assertEqual(() if expected == "none" else ("a.py",), paths)

    def test_creation_variants_history_and_restart(self):
        cases = ("parents", "explicit", "existing", "separate", "partial", "historical", "legacy", "recovery")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace = root / "workspace"
                workspace.mkdir()
                store = SessionStore(root / "state.db")
                store.initialize(workspace)
                record = store.create("test", workspace, "openai", "offline")
                config = AppConfig(workspace=workspace,
                    provider=ProviderConfig("openai", "synthetic", "https://example.invalid", "offline"),
                    audit_dir=root / "audit", plan_enabled=False,
                    memory=MemoryConfig(compaction="off", persistence="off"))
                if case in {"historical", "legacy"}:
                    store.save_memory(record.id, SessionMemory(
                        verification_obligation="legacy_unknown" if case == "legacy" else "pending",
                        pending_verification_paths=("app",)))
                actions = []
                if case == "explicit":
                    actions.append(("create_directory", {"path": "app/demo", "parents": True}))
                if case == "existing":
                    (workspace / "app/demo").mkdir(parents=True)
                actions.append(create("app/demo/main.py"))
                if case == "recovery":
                    actions.extend([("run_command", {"command": "python app/demo/main.py"}), FINISH])
                if case in {"separate", "partial"}:
                    actions.append(create("app/demo/other.py"))
                actions.append(check("main.py", "app/demo"))
                if case == "separate":
                    actions.append(check("app/demo/other.py"))
                actions.append(FINISH)
                if case == "partial":
                    actions.append(FINISH)
                provider = RecordingProvider([response(str(i), *action) for i, action in enumerate(actions)])
                def runtime():
                    return SessionRuntime(SessionStore(root / "state.db"), workspace,
                        initial_session_id=record.id, config_loader=lambda **_: config,
                        provider_factory=lambda *_: provider, options=RuntimeOptions(environ={}),
                        approver=lambda *_: True, workspace_confirmer=lambda _: True)
                active = runtime()
                try:
                    result = active.run_task("合成创建与检查")
                    self.assertEqual(case != "partial", result.ok, result)
                    expected = ("app",) if case in {"historical", "legacy"} else ()
                    if case == "partial":
                        self.assertIn("app/demo/other.py", result.pending_verification_paths)
                        self.assertIn("app", result.pending_verification_paths)
                        expected = result.pending_verification_paths
                    self.assertEqual(expected, result.pending_verification_paths)
                    self.assertFalse(list(workspace.rglob("*.pyc")))
                    if case == "legacy":
                        self.assertEqual("legacy_unknown", result.verification_obligation)
                finally:
                    self.assertTrue(active.close())
                provider.responses.append(response("review", *FINISH))
                restored = runtime()
                try:
                    self.assertEqual(expected, restored.current.memory.pending_verification_paths)
                    self.assertTrue(restored.run_task("只读回顾").ok)
                    self.assertEqual(expected, restored.current.memory.pending_verification_paths)
                    self.assertEqual(len(actions)+1, len(provider.histories))
                finally:
                    restored.close()

    def test_runtime_rejects_obsolete_and_non_verifying_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.py").write_text("x=1", encoding="utf8")
            scope = VerificationScope()
            scope.begin_task("task-1")
            good = scope.issue_command_check(argv=("python", "check"), cwd=".", kind="syntax",
                returncode=0, output_summary="", execution_complete=True, workspace_stable=True,
                targets=("a.py",), snapshot_id="current")
            for last in (replace(good, returncode=1), replace(good, kind="script"),
                         replace(good, kind="other"), replace(good, diagnostics=("scope_filtered",)),
                         replace(good, kind="tests", diagnostics=("zero_tests_reported",)),
                         replace(good, snapshot_id="old")):
                result = RunResult(ok=True, summary="", rounds=1, tool_calls=1,
                    task_validation=TaskValidationReport(records=(good, last), status="observed"))
                obligation, paths = _merge_verification_obligation(
                    SessionMemory(verification_obligation="pending", pending_verification_paths=("a.py",)),
                    FileEffects(EffectState.NONE), result, trusted_pass=True,
                    workspace_policy=WorkspacePolicy(root), verification_scope=scope,
                    current_snapshot_id="current")
                self.assertEqual(("pending", ("a.py",)), (obligation, paths))
