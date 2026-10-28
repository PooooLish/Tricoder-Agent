"""Delivery hints through real tools and SessionRuntime, without model calls."""
import json
import unittest

from delivery_helpers import DeliveryHarness, ORIGINAL, FIXED, TESTS, call, edit, check, finish
from tricoder.patches import PatchError
from tricoder.tools.write import _patch_feedback


class ToolGuidanceTests(unittest.TestCase):
    def test_structural_errors_offer_precise_non_echoing_alternative(self):
        for code in ('patch_invalid_header', 'patch_invalid_hunk',
                     'patch_count_mismatch', 'patch_invalid_format'):
            with self.subTest(code=code):
                text = _patch_feedback(PatchError('PRIVATE /absolute/path', detail_code=code))
                for phrase in ('edit_file', 'old_text', '非空', '唯一', 'read_file', '多文件', '不要原样'):
                    self.assertIn(phrase, text)
                self.assertNotIn('PRIVATE', text)
                self.assertLess(len(text), 800)

    def test_bad_patch_to_edit_check_finish_both_protocols(self):
        for protocol in ('native', 'legacy_json'):
            actions = [[call('read_file', path='config.py')],
                       [call('apply_patch', patch='*** Begin Patch\nPRIVATE\n*** End Patch')],
                       edit(), check(), finish()]
            with self.subTest(protocol=protocol), DeliveryHarness(actions, protocol=protocol) as h:
                observed = []
                def inspect(n):
                    if n == 3:
                        observed.append((len(h.approvals), (h.workspace/'config.py').read_text()))
                        text = '\n'.join((m.content or "") for m in h.provider.histories[-1])
                        self.assertIn('不要原样', text)
                        self.assertIn('old_text', text)
                h.provider.before_response = inspect
                result = h.run()
                self.assertTrue(result.ok, result)
                self.assertEqual([(0, ORIGINAL)], observed)
                self.assertGreaterEqual(len(h.approvals), 2)
                self.assertEqual(FIXED, (h.workspace/'config.py').read_text())

    def test_existing_file_can_reuse_current_read_without_extra_read(self):
        for protocol in ('native', 'legacy_json'):
            actions = [[call('read_file', path='config.py')],
                       [call('create_file', path='config.py', content='PRIVATE')],
                       edit(), check(), finish()]
            with self.subTest(protocol=protocol), DeliveryHarness(actions, protocol=protocol) as h:
                result = h.run()
                self.assertTrue(result.ok, result)
                text = '\n'.join((m.content or "") for m in h.provider.histories[2])
                self.assertIn('仅在', text)
                self.assertIn('已知', text)
                self.assertNotIn('请先 read_file，再', text)



class VerificationGuidanceTests(unittest.TestCase):
    def test_targeted_success_then_read_does_not_cover_root_root_check_then_finish(self):
        for protocol in ('native','legacy_json'):
            actions=[edit(),check('python -m unittest discover -s tests -v'),
                [call('read_file',path='config.py')],check('python -m unittest discover -v'),finish()]
            with self.subTest(protocol=protocol), DeliveryHarness(actions,protocol=protocol) as h:
                (h.workspace/'tests').mkdir()
                (h.workspace/'tests/__init__.py').write_text('')
                (h.workspace/'tests/test_config.py').write_text(TESTS)
                result=h.run()
                self.assertTrue(result.ok,result)
                partial=[json.loads(m.content) for m in h.provider.histories[2] if m.kind=='verification_feedback'][-1]
                self.assertEqual('succeeded',partial['execution_status'])
                self.assertEqual(['config.py'],partial['pending_paths'])
                self.assertEqual('verify_pending_paths',partial.get('next_step_hint'))
                self.assertIn('读文件不能',partial['message'])
                self.assertIn('python -m unittest discover -v',partial['message'])
                after_read=[json.loads(m.content) for m in h.provider.histories[3] if m.kind=='verification_feedback'][-1]
                self.assertEqual(partial,after_read)
                final=h.provider.feedback()[-1]
                self.assertEqual('finish_if_task_complete',final.get('next_step_hint'))
                self.assertIn('若用户任务也已完成',final['message'])

    def test_finish_correction_explains_coverage_without_clearing_it(self):
        with DeliveryHarness([edit(),finish(),[call('read_file',path='config.py')],finish()],max_rounds=4) as h:
            result=h.run()
            self.assertFalse(result.ok)
            feedback=[json.loads(m.content) for m in h.provider.histories[2] if m.kind=='verification_feedback'][-1]
            self.assertEqual('verify_pending_paths',feedback.get('next_step_hint'))
            self.assertEqual(1,feedback['recovery_count'])
            self.assertEqual(['config.py'],feedback['pending_paths'])

    def test_old_failure_and_zero_tests_keep_priority(self):
        for failed in (False,True):
            actions=[edit(),check('python -m unittest discover -s tests -v'),check(),finish()]
            with self.subTest(failed=failed), DeliveryHarness(actions,max_rounds=4) as h:
                (h.workspace/'tests').mkdir()
                if failed: (h.workspace/'tests/test_bad.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("synthetic")\n')
                result=h.run()
                feedback=[json.loads(m.content) for m in h.provider.histories[2] if m.kind=='verification_feedback'][-1]
                self.assertNotEqual('verify_pending_paths',feedback.get('next_step_hint'))
                if failed:
                    self.assertEqual('repair_blocking_checks',feedback.get('next_step_hint'))
                    self.assertTrue(feedback['blocking_checks'])
                    self.assertFalse(result.ok)
                else:
                    self.assertEqual('zero_tests_reported',feedback['reason_code'])
                    self.assertIn('零测试',feedback.get('message',''))

    def test_pending_hint_is_bounded_redacted_and_does_not_change_state(self):
        from tricoder.engine.state import AgentRunState
        from tricoder.engine.verification_feedback import finish_recovery_feedback
        from tricoder.models import SessionContext
        state=AgentRunState.start('system','task',SessionContext())
        state.verification_required=True
        state.task_modified_files=['src/'+str(i)+'x'*200+'.py' for i in range(40)]
        state.task_modified_files += ['.env.local','C:/private/secret','../escape']
        report=state.validation.report()
        text=finish_recovery_feedback(state)
        payload=json.loads(text)
        self.assertLessEqual(len(text),2000)
        self.assertLessEqual(len(payload['pending_paths']),20)
        self.assertEqual(40,payload['total_path_count'])
        self.assertEqual(40-len(payload['pending_paths']),payload['omitted_count'])
        self.assertEqual('verify_pending_paths',payload['next_step_hint'])
        for secret in ('.env','C:/','private','../escape'): self.assertNotIn(secret,text)
        self.assertNotIn('src/',payload['message'])
        self.assertEqual(report,state.validation.report())
        self.assertIsNone(state.evidence)
        self.assertEqual(0,state.finish_verification_recoveries)

    def test_unknown_cleanup_and_stale_never_get_coverage_hint(self):
        from tricoder.engine.state import AgentRunState
        from tricoder.engine.verification_feedback import finish_recovery_feedback
        from tricoder.models import SessionContext
        for attribute,value in (('unknown_effects',True),('cleanup_failed',True),('verification_obligation','legacy_unknown')):
            state=AgentRunState.start('system','task',SessionContext())
            state.verification_required=True; state.task_modified_files=['app.py']
            setattr(state,attribute,value)
            payload=json.loads(finish_recovery_feedback(state))
            self.assertNotIn('next_step_hint',payload)

    def test_last_round_real_business_check_is_not_automatic_finish(self):
        actions=[edit(),check('python -m unittest discover -v')]
        with DeliveryHarness(actions,max_rounds=2) as h:
            (h.workspace/'tests').mkdir()
            (h.workspace/'tests/__init__.py').write_text('')
            (h.workspace/'tests/test_config.py').write_text(TESTS)
            result=h.run()
            self.assertFalse(result.ok)
            self.assertEqual('round_budget_exhausted',result.stop_reason)
            self.assertEqual(2,len(h.provider.histories))
            records=result.task_validation.records
            self.assertTrue(any(r.kind=='tests' and r.returncode==0 for r in records))

    def test_multiple_tools_share_one_budget_round(self):
        from test_round_budget_feedback import budgets
        actions=[[call('read_file',path='config.py'),call('list_files',path='.')],finish()]
        with DeliveryHarness(actions,max_rounds=2) as h:
            self.assertTrue(h.run('read then finish').ok)
            self.assertEqual([1,2],[d['round_number'] for _,d in budgets(h.provider.histories[-1])])
