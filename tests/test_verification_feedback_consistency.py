"""Feedback cannot accept a check while the trusted task still has old failures."""
from dataclasses import replace
import json
import unittest

from delivery_helpers import DeliveryHarness, TESTS, call, check, edit, finish, reconstructed
from tricoder.engine.state import AgentRunState
from tricoder.engine.verification_feedback import checkpoint, build_batch_feedback
from tricoder.models import SessionContext
from tricoder.workspace.verification import VerificationScope, WorkspaceSnapshot


class FeedbackConsistencyTests(unittest.TestCase):
    def test_successful_root_check_does_not_hide_old_failed_discovery(self):
        with DeliveryHarness(reconstructed() + [finish()], max_rounds=7) as h:
            result = h.run()
            feedback = h.provider.feedback()[-1]
            self.assertFalse(result.ok)
            self.assertEqual("pending", feedback["verification_obligation"])
            self.assertEqual([], feedback["pending_paths"])
            self.assertEqual("failed", feedback["validation_status"])
            self.assertEqual(1, feedback["unresolved_check_count"])
            self.assertEqual("unresolved_checks", feedback["reason_code"])

    def test_retry_exact_check_clears_feedback_and_allows_finish(self):
        actions = reconstructed() + [check("python -m unittest discover -s tests -t . -v"), finish()]
        with DeliveryHarness(actions) as h:
            result = h.run()
            self.assertTrue(result.ok, result)
            feedback = h.provider.feedback()[-1]
            self.assertEqual("satisfied", feedback["verification_obligation"])
            self.assertEqual(0, feedback["unresolved_check_count"])

    def test_zero_tests_and_targeted_tests_do_not_claim_root_coverage(self):
        actions = [edit(), check("python -m unittest -q"),
            [call("create_file", path="tests/test_config.py", content=TESTS, create_parents=True)],
            check("python -m unittest discover -s tests -v"), check(), finish()]
        with DeliveryHarness(actions) as h:
            result = h.run()
            feedback = h.provider.feedback()
            zero = next(f for f in feedback if f["check_kind"] == "tests")
            self.assertEqual("zero_tests_reported", zero["reason_code"])
            self.assertEqual("pending", zero["verification_obligation"])
            targeted = [f for f in feedback if f["check_kind"] == "tests"][-1]
            self.assertIn("config.py", targeted["pending_paths"])
            self.assertEqual("pending", targeted["verification_obligation"])
            # The zero-test history stays stale, but current tests+syntax cover the changes.
            self.assertTrue(result.ok, result)
            self.assertTrue(result.task_validation.stale_check_ids)
            self.assertEqual("satisfied", feedback[-1]["verification_obligation"])

    def test_targeted_tests_and_compile_union_still_succeeds(self):
        actions = [edit(), [call("create_file", path="tests/test_config.py", content=TESTS, create_parents=True)],
            check("python -m unittest discover -s tests -v"), check(), finish()]
        with DeliveryHarness(actions) as h:
            self.assertTrue(h.run().ok)
            self.assertEqual("satisfied", h.provider.feedback()[-1]["verification_obligation"])

    def test_pure_confirmation_needs_no_check_in_both_protocols(self):
        for protocol in ("native", "legacy_json"):
            with self.subTest(protocol=protocol), DeliveryHarness([finish()], protocol=protocol) as h:
                self.assertTrue(h.run("只确认初始决定 red，不修改文件").ok)
                self.assertEqual([], h.provider.feedback())

    def test_feedback_is_read_only_and_rejects_forged_authority(self):
        state = AgentRunState.start("system", "task", SessionContext())
        state.verification_required = True
        state.verification = "通过"
        state.task_modified_files = ["config.py"]
        scope = VerificationScope()
        scope.begin_task(state.current_task_id)
        # Only a host-bound scope can validate authority, never model-supplied state.
        if hasattr(state, "verification_scope"):
            state.verification_scope = scope
        snap = WorkspaceSnapshot(scope.token, "current", True)
        state.evidence = scope.issue(snap, snap, True)
        before = checkpoint(state)
        record = scope.issue_command_check(argv=("python", "-m", "compileall", "."), cwd=".", kind="syntax",
            returncode=0, output_summary="", execution_complete=True, workspace_stable=True,
            targets=(".",), snapshot_id="current")
        state.validation.observe(record)
        state.evidence = replace(state.evidence, _authority=object())
        report, evidence = state.validation.report(), state.evidence
        feedback = json.loads(build_batch_feedback(state, before))
        self.assertNotEqual("satisfied", feedback["verification_obligation"])
        self.assertIs(evidence, state.evidence)
        self.assertEqual(report, state.validation.report())
        self.assertEqual("通过", state.verification)
