"""Only a pre-approval existing ordinary file is a recoverable usage error."""
from pathlib import Path
import tempfile
import unittest

from delivery_helpers import ROOT, ORIGINAL, DeliveryHarness, call, check, edit, finish
from tricoder.execution_state import EffectState, ErrorCode, RecoveryAction
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry


class CreateFileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime", prefix="create-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "exists.py"
        self.target.write_text(ORIGINAL, encoding="utf8")
        self.approvals = []
        self.registry = ToolRegistry(ToolContext(WorkspacePolicy(self.root), CommandPolicy(self.root),
            approver=lambda *args: self.approvals.append(args) or True))

    def create(self, path="exists.py"):
        return self.registry.execute("create_file", {"path": path, "content": "replacement"})

    def test_existing_file_is_recoverable_without_approval_or_effects(self):
        result = self.create()
        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
        self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
        self.assertEqual("file_already_exists", result.error.detail_code)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertEqual([], self.approvals)
        self.assertEqual(ORIGINAL, self.target.read_text(encoding="utf8"))
        self.assertEqual(["exists.py"], [p.name for p in self.root.iterdir()])
        self.assertIn("read_file", result.output)
        self.assertNotIn("exists.py", result.output)

    def test_same_runtime_recovers_by_edit_check_finish_in_both_protocols(self):
        for protocol in ("native", "legacy_json"):
            actions = [[call("create_file", path="config.py", content="replacement")], edit(), check(), finish()]
            with self.subTest(protocol=protocol), DeliveryHarness(actions, protocol=protocol) as h:
                result = h.run()
                self.assertTrue(result.ok, result)
                self.assertEqual(4, result.rounds)
                self.assertFalse(result.unknown_effects or result.cleanup_failed)
                self.assertIn("file_already_exists", str(h.provider.histories[1]))

    def test_directory_sensitive_path_outside_and_readonly_stay_strict(self):
        (self.root / "folder").mkdir()
        for path in ("folder", ".git/config", "../outside.py"):
            with self.subTest(path=path):
                result = self.create(path)
                self.assertEqual(ErrorCode.POLICY_DENIED, result.error.code)
                self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
                self.assertIsNone(result.error.detail_code)
        self.registry.context.read_only = True
        self.assertEqual(ErrorCode.POLICY_DENIED, self.create().error.code)
        self.assertEqual([], self.approvals)

    def test_target_appearing_during_approval_is_not_recoverable_or_overwritten(self):
        target = self.root / "new.py"
        def approve(*args):
            target.write_text("racing writer", encoding="utf8")
            return True
        self.registry.context.approver = approve
        result = self.create("new.py")
        self.assertFalse(result.ok)
        self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
        self.assertNotEqual("file_already_exists", result.error.detail_code)
        self.assertEqual("racing writer", target.read_text(encoding="utf8"))

    def test_internal_symlink_remains_strict(self):
        alias = self.root / "alias.py"
        try:
            alias.symlink_to(self.target)
        except OSError:
            self.skipTest("当前账户不能创建符号链接")
        result = self.create("alias.py")
        self.assertEqual(ErrorCode.POLICY_DENIED, result.error.code)
        self.assertIsNone(result.error.detail_code)
        self.assertEqual([], self.approvals)
