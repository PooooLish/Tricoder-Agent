"""Eval observation clock cannot change scoring, exceptions or cleanup order."""
from contextlib import ExitStack
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_eval_runner as fixtures
from tricoder.evals import runner
from tricoder.evals.models import PlannedTrial, TrialKey, EvalCondition
from tricoder.evals.service import _trial_from_case_result


class TimingTests(unittest.TestCase):
    setUp=fixtures.EvalRunnerTests.setUp
    tearDown=fixtures.EvalRunnerTests.tearDown
    _make_case=fixtures.EvalRunnerTests._make_case
    _passing_executor=staticmethod(fixtures.EvalRunnerTests._passing_executor)

    def run_observed(self, *, prepare_error=False, executor_error=None, cleanup_error=False, verification_error=None):
        now=[0]; events=[]
        def wrap(name,ms,fn,error=None):
            def call(*args,**kwargs):
                events.append(name); now[0]+=ms*1_000_000
                if error is not None: raise error
                return fn(*args,**kwargs)
            return call
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner.time,'perf_counter_ns',side_effect=lambda:now[0]))
            stack.enter_context(patch.object(runner.time,'perf_counter',side_effect=lambda:now[0]/1e9))
            for name,ms,error in (('prepare_workspace',10,OSError() if prepare_error else None),
                                   ('capture_snapshot',10,None),('remove_verifier',20,OSError() if cleanup_error else None),
                                   ('install_verifier',5,None),('_run_verification',15,None)):
                fn=getattr(runner,name)
                if name=='_run_verification' and verification_error:
                    fn=lambda *a: runner.VerificationResult('hidden',None,False,verification_error)
                stack.enter_context(patch.object(runner,name,side_effect=wrap(name,ms,fn,error)))
            executor=wrap('agent',30,self._passing_executor,executor_error)
            result=runner.run_suite(self.suite,self.run_dir,'fake','offline',executor).cases[0]
        return result,events

    def test_exact_phases_include_final_cleanup_and_keep_legacy_duration(self):
        result,events=self.run_observed()
        self.assertIsNotNone(getattr(result,'timings',None),'Eval phase observations are missing')
        self.assertEqual((100,30,20,50),(result.timings.total_ms,result.timings.agent_ms,result.timings.verifier_ms,result.timings.harness_ms))
        self.assertEqual(80,result.duration_ms)
        self.assertEqual('remove_verifier',events[-1]);self.assertEqual('passed',result.status)
        condition=EvalCondition('one','fake','offline','quality','off','off',(),())
        record=_trial_from_case_result(PlannedTrial(1,TrialKey('one',self.case.id,1),self.case,condition),result)
        self.assertEqual(result.timings,record.timings)

    def test_preparation_failure_observes_zero_unstarted_phases(self):
        r,events=self.run_observed(prepare_error=True)
        self.assertIsNotNone(getattr(r,'timings',None))
        self.assertEqual((10,0,0,10),(r.timings.total_ms,r.timings.agent_ms,r.timings.verifier_ms,r.timings.harness_ms))
        self.assertEqual(('workspace_error',),r.failure_codes);self.assertEqual(['prepare_workspace'],events)

    def test_executor_error_retains_cleanup_and_partial_observations(self):
        r,events=self.run_observed(executor_error=RuntimeError('offline'))
        self.assertIsNotNone(getattr(r,'timings',None))
        self.assertEqual((70,30,0,40),(r.timings.total_ms,r.timings.agent_ms,r.timings.verifier_ms,r.timings.harness_ms))
        self.assertEqual(('executor_error',),r.failure_codes);self.assertEqual('remove_verifier',events[-1])

    def test_cleanup_failure_remains_error(self):
        r,events=self.run_observed(cleanup_error=True)
        self.assertIsNotNone(getattr(r,'timings',None))
        self.assertEqual('error',r.status);self.assertEqual(('workspace_error',),r.failure_codes)
        self.assertEqual(100,r.timings.total_ms)

    def test_hidden_timeout_is_not_hidden_by_timing(self):
        r,_=self.run_observed(verification_error='verification_timeout')
        self.assertIsNotNone(getattr(r,'timings',None))
        self.assertEqual('verification_timeout',r.verifications[0].error_code)
        self.assertNotEqual('passed',r.status)

    def test_keyboard_interrupt_propagates_and_cleans(self):
        with patch.object(runner,'remove_verifier',wraps=runner.remove_verifier) as cleanup:
            with self.assertRaises(KeyboardInterrupt): self.run_observed(executor_error=KeyboardInterrupt())
            self.assertEqual(1,cleanup.call_count)

    def test_async_cancellation_propagates_without_normal_error_result(self):
        import asyncio
        with self.assertRaises(asyncio.CancelledError):
            self.run_observed(executor_error=asyncio.CancelledError())
        self.assertFalse((self.run_dir/'workspaces'/self.case.id/fixtures.RESERVED_VERIFIER_DIR).exists())

    def test_verifier_install_failure_records_stage_and_cleans(self):
        with patch.object(runner,'install_verifier',side_effect=OSError('synthetic')):
            r,events=self.run_observed()
        self.assertEqual('error',r.status)
        self.assertEqual(5,r.timings.verifier_ms)
        self.assertEqual('remove_verifier',events[-1])

    def test_legacy_constructor_does_not_invent_timings(self):
        r=runner.EvalCaseResult('case','error',(),1,0,0,(),(),'not-run',None)
        self.assertIsNone(getattr(r,'timings',None))


if __name__=='__main__':unittest.main()
