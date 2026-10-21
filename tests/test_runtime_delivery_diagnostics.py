"""Runtime attribution must describe failure without accepting a failed baseline."""
import unittest
from unittest.mock import patch

from delivery_helpers import DeliveryHarness, check, edit, finish
import tricoder.session.runtime as runtime_module


class RuntimeDeliveryDiagnosticsTests(unittest.TestCase):
    def test_budget_failure_is_attributable_but_requires_confirmation(self):
        observed = []
        real = runtime_module.task_changes_match_baselines

        def record(*args, **kwargs):
            result = real(*args, **kwargs)
            observed.append(result)
            return result

        with DeliveryHarness([edit(), check(), finish()], max_rounds=2, confirmer=False) as h:
            with patch.object(runtime_module, "task_changes_match_baselines", side_effect=record):
                result = h.run()
            self.assertTrue(observed and all(observed))
            self.assertFalse(result.ok)
            self.assertIn("轮", result.summary)
            self.assertNotIn("未归属", result.summary)
            self.assertIn("尚未完整交付", result.summary)
            self.assertTrue(h.runtime._workspace_baseline_unresolved)
            self.assertEqual("待验证", result.verification)
            self.assertEqual("stale", result.task_validation.status)
            calls = len(h.provider.histories)
            with self.assertRaises(runtime_module.WorkspaceOperationCancelled):
                h.run("只回顾")
            self.assertEqual(calls, len(h.provider.histories))

    def test_model_incomplete_keeps_original_reason_and_confirmation(self):
        with DeliveryHarness([edit(), check(), finish("incomplete")]) as h:
            result = h.run()
            self.assertFalse(result.ok)
            self.assertIn("synthetic delivery", result.summary)
            self.assertNotIn("未归属", result.summary)
            self.assertTrue(h.runtime._workspace_baseline_unresolved)

    def test_external_write_is_still_unattributed(self):
        with DeliveryHarness([edit(), check(), finish()]) as h:
            def external(index):
                if index == 3:
                    (h.workspace / "external.py").write_text("external = True\n", encoding="utf8")
            h.provider.before_response = external
            result = h.run()
            self.assertFalse(result.ok)
            self.assertIn("未归属", result.summary)
            self.assertTrue(h.runtime._workspace_baseline_unresolved)

    def test_success_advances_baseline_without_extra_confirmation(self):
        with DeliveryHarness([edit(), check(), finish(), finish()]) as h:
            self.assertTrue(h.run().ok)
            confirmations = len(h.confirmations)
            self.assertFalse(h.runtime._workspace_baseline_unresolved)
            self.assertTrue(h.run("只回顾").ok)
            self.assertEqual(confirmations, len(h.confirmations))
