"""Current coverage must supersede non-failing history through the real Runtime."""
import unittest

from delivery_helpers import DeliveryHarness, TESTS, call, check, edit, finish


CREATE_TEST = [call('create_file', path='tests/test_config.py', content=TESTS, create_parents=True)]
TARGET_TEST = check('python -m unittest discover -s tests -v')


class CurrentValidationRuntimeTests(unittest.TestCase):
    def assert_completed_without_carryover(self, actions, protocol='native'):
        with DeliveryHarness(actions, protocol=protocol) as h:
            result = h.run()
            self.assertTrue(result.ok, result)
            self.assertEqual('completed', result.stop_reason.value)
            self.assertEqual((), result.pending_verification_paths)
            self.assertEqual('none', result.verification_obligation)
            self.assertEqual((), h.runtime.current.memory.pending_verification_paths)
            self.assertEqual('satisfied', h.provider.feedback()[-1]['verification_obligation'])
            self.assertTrue(result.task_validation.stale_check_ids)
            self.assertEqual('observed', result.task_validation.status)
            count = len(h.confirmations)
            h.provider.actions.append(finish())
            self.assertTrue(h.run('只读确认').ok)
            self.assertEqual(count, len(h.confirmations))
            self.assertEqual((), h.runtime.current.memory.pending_verification_paths)

    def test_old_success_different_command_current_coverage(self):
        for protocol in ('native', 'legacy_json'):
            with self.subTest(protocol=protocol):
                self.assert_completed_without_carryover(
                    [check(), edit(), check('python -m compileall -q .'), finish()], protocol)

    def test_old_successful_script_does_not_create_replay_obligation(self):
        self.assert_completed_without_carryover(
            [check('python config.py'), edit(), check('python -m compileall -q .'), finish()])

    def test_old_zero_tests_then_current_union(self):
        self.assert_completed_without_carryover(
            [edit(), check('python -m unittest -q'), CREATE_TEST, TARGET_TEST, check(), finish()])

    def test_real_failure_not_cleared_by_different_success(self):
        with DeliveryHarness([CREATE_TEST, TARGET_TEST, edit(), check('python -m compileall -q .'), finish()],
                             max_rounds=5) as h:
            result = h.run()
            self.assertFalse(result.ok)
            self.assertEqual(1, len(result.task_validation.unresolved_check_ids))
            self.assertEqual('failed', result.task_validation.status)
            self.assertIn('config.py', result.pending_verification_paths)
            self.assertEqual('pending', h.provider.feedback()[-1]['verification_obligation'])

    def test_real_failure_exact_retest_restores_completion(self):
        with DeliveryHarness([CREATE_TEST, TARGET_TEST, edit(), TARGET_TEST, check(), finish()]) as h:
            result = h.run()
            self.assertTrue(result.ok, result)
            self.assertEqual((), result.task_validation.unresolved_check_ids)
            self.assertEqual((), result.pending_verification_paths)
