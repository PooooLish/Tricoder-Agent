"""Blocking identity is actionable without echoing arbitrary command material."""
import json
import unittest

from delivery_helpers import DeliveryHarness, call, check, edit, finish, TESTS
from tricoder.engine.state import AgentRunState
from tricoder.engine.verification_feedback import checkpoint, build_batch_feedback
from tricoder.models import SessionContext
from tricoder.workspace.verification import VerificationScope, WorkspaceSnapshot


class BlockingCheckFeedbackTests(unittest.TestCase):
    def test_read_only_failed_review_has_facts_but_no_blocking_checks(self):
        with DeliveryHarness([check('python -m unittest discover -s tests -v'), finish()]) as h:
            (h.workspace / 'tests').mkdir()
            (h.workspace / 'tests/test_config.py').write_text(TESTS, encoding='utf8')
            result = h.run('只读审查，报告已有测试失败，不修改文件')
            self.assertTrue(result.ok, result)
            feedback = h.provider.feedback()[-1]
            self.assertEqual('none', feedback['verification_obligation'])
            self.assertEqual(1, feedback['unresolved_check_count'])
            self.assertEqual([], feedback['blocking_checks'])
            self.assertEqual(0, feedback['blocking_checks_omitted_count'])
            self.assertNotIn('仍须修复', feedback.get('message', ''))

    def test_redacted_pending_path_does_not_mean_coverage_complete(self):
        state = AgentRunState.start('system', 'task', SessionContext())
        scope = VerificationScope()
        scope.begin_task(state.current_task_id)
        state.verification_required = True
        state.task_modified_files = ['.env.local']
        before = checkpoint(state)
        state.validation.observe(scope.issue_command_check(argv=('python', 'test.py'), cwd='.',
            kind='tests', returncode=1, output_summary='', execution_complete=True,
            workspace_stable=True, targets=('tests',), snapshot_id='now'))
        payload = json.loads(build_batch_feedback(state, before))
        self.assertEqual([], payload['pending_paths'])
        self.assertNotIn('覆盖已齐全', payload['message'])
        self.assertNotIn('.env.local', json.dumps(payload))

    def test_runtime_identifies_unresolved_check_with_full_coverage_both_protocols(self):
        actions = [[call('create_file', path='tests/test_config.py', content=TESTS, create_parents=True)],
                   check('python -m unittest discover -s tests -v'), edit(),
                   check('python -m compileall -q .'), finish()]
        for protocol in ('native', 'legacy_json'):
            with self.subTest(protocol=protocol), DeliveryHarness(actions, protocol=protocol, max_rounds=5) as h:
                result = h.run()
                self.assertFalse(result.ok)
                feedback = h.provider.feedback()[-1]
                self.assertEqual([], feedback['pending_paths'])
                failed_id = result.task_validation.unresolved_check_ids[0]
                self.assertEqual([{'check_id': failed_id, 'kind': 'tests', 'reason': 'failed',
                                   'targets': ['tests']}], feedback.get('blocking_checks'))
                self.assertEqual(1, feedback['unresolved_check_count'])
                self.assertEqual(1, feedback['historical_stale_count'])
                self.assertIn('覆盖已齐全', feedback['message'])
                self.assertNotIn('compileall', feedback['message'])

    def test_bounded_redacted_read_only_and_evicted_failure_ids(self):
        state = AgentRunState.start('system', 'task', SessionContext())
        scope = VerificationScope()
        scope.begin_task(state.current_task_id)
        state.verification_scope = scope
        state.verification_required = True
        state.task_modified_files = [f'src/file_{i}.py' for i in range(30)]
        before = checkpoint(state)
        ids = []
        for i in range(38):
            record = scope.issue_command_check(
                argv=('python', 'sensitive_argument_' + 'X' * 4000, str(i)), cwd='.', kind='tests',
                returncode=1, output_summary='SECRET_OUTPUT', execution_complete=True,
                workspace_stable=True, targets=('.env.local', 'C:/private/key.txt', '../escape',
                    'tests/secret_token=private.py', 'tests/test_ok.py'), snapshot_id='now')
            state.validation.observe(record)
            ids.append(record.check_id)
        report = state.validation.report()
        evidence, failed = state.evidence, state.failed_snapshot
        text = build_batch_feedback(state, before)
        payload = json.loads(text)
        self.assertEqual(38, payload['unresolved_check_count'])
        self.assertEqual(5, len(payload.get('blocking_checks', [])))
        self.assertEqual(33, payload['blocking_checks_omitted_count'])
        self.assertEqual(ids[:5], [item['check_id'] for item in payload['blocking_checks']])
        for item in payload['blocking_checks']:
            self.assertEqual('record_unavailable', item['reason'])
            self.assertIsNone(item['kind'])
            self.assertEqual([], item['targets'])
        self.assertLessEqual(len(text), 2000)
        for secret in ('SECRET_OUTPUT', 'sensitive_argument', '.env.local', 'C:/', '../escape', 'private'):
            self.assertNotIn(secret, text)
        self.assertEqual(report, state.validation.report())
        self.assertIs(evidence, state.evidence)
        self.assertIs(failed, state.failed_snapshot)
        # Retained records still omit unsafe targets and never echo argv/output.
        before = checkpoint(state)
        state.validation.observe(scope.issue_command_check(
            argv=('python', 'more'), cwd='.', kind='tests', returncode=1, output_summary='SECRET_OUTPUT',
            execution_complete=True, workspace_stable=True, targets=('tests/test_ok.py',), snapshot_id='now'))
        # Resolve only the evicted signatures using real tracker rules, not feedback mutation.
        for i in range(6):
            state.validation.observe(scope.issue_command_check(
                argv=('python', 'sensitive_argument_' + 'X' * 4000, str(i)), cwd='.', kind='tests',
                returncode=0, output_summary='', execution_complete=True, workspace_stable=True,
                targets=('tests',), snapshot_id='now'))
        payload = json.loads(build_batch_feedback(state, before))
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False, separators=(',', ':'))), 2000)
        self.assertNotIn('SECRET_OUTPUT', json.dumps(payload))

    def test_retained_targets_and_incomplete_failure_are_sanitized(self):
        state = AgentRunState.start('system', 'task', SessionContext())
        scope = VerificationScope()
        scope.begin_task(state.current_task_id)
        state.verification_scope = scope
        state.verification_required = True
        before = checkpoint(state)
        state.validation.observe(scope.issue_command_check(argv=('python', '--token=HIDDEN'),
            cwd='.', kind='tests', returncode=None, output_summary='HIDDEN', execution_complete=False,
            workspace_stable=False, targets=('tests/test_ok.py', '.env.local', 'C:/private',
                '../escape', 'tests/password=HIDDEN.py', 'a' * 500)))
        text = build_batch_feedback(state, before)
        payload = json.loads(text)
        self.assertIn('blocking_checks', payload)
        self.assertEqual('incomplete', payload['blocking_checks'][0]['reason'])
        self.assertEqual(['tests/test_ok.py'], payload['blocking_checks'][0]['targets'])
        self.assertNotIn('HIDDEN', text)
        self.assertNotIn('.env', text)
