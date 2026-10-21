"""Frozen offline budget preparation; no Provider or configuration loading."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from delivery_helpers import ROOT, DeliveryHarness, reconstructed, call, check, finish
from tricoder.evals.loader import load_experiment, load_suite
from tricoder.evals.experiment import plan_trials, _experiment_fingerprints
from tricoder.evals.compare import ComparisonError, compare_report_payloads
from test_eval_compare import _report, _trial


class BudgetExperimentTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('prepare_budget',ROOT/'scripts/prepare_delivery_budget_experiment.py')
        self.module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='budget-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_only_quality_budget_diff_same_code_and_two_19_trial_plans(self):
        out=self.root/'frozen'
        # A network/config call is never needed to generate these files.
        with patch('tricoder.config.load_config',side_effect=AssertionError('config forbidden')):
            self.module.prepare(ROOT/'evals/quality-v2.2',out)
        definitions=[load_experiment(out/f'budget{n}/experiment.toml') for n in (8,16)]
        self.assertEqual([19,19],[len(plan_trials(d)) for d in definitions])
        self.assertEqual([p.key for p in plan_trials(definitions[0])],[p.key for p in plan_trials(definitions[1])])
        source=load_suite(ROOT/'evals/quality-v2.2')
        for original,a,b in zip(source.cases,definitions[0].suite.cases,definitions[1].suite.cases):
            self.assertEqual(8,a.max_rounds)
            self.assertEqual(16 if b.execution_kind=='quality' else 8,b.max_rounds)
            before=(original.source_dir/'case.toml').read_bytes()
            self.assertEqual(before,(a.source_dir/'case.toml').read_bytes())
            expected=before.replace(b'max_rounds = 8',b'max_rounds = 16') if b.execution_kind=='quality' else before
            self.assertEqual(expected,(b.source_dir/'case.toml').read_bytes())
        delta=json.loads((out/'allowed-delta.json').read_text(encoding='utf8'))
        self.assertEqual(19,len(delta['case_changes']))
        hashes=json.loads((out/'source-sha256.json').read_text(encoding='utf8'))
        self.assertTrue(hashes['code'])
        self.assertTrue((out/'code/src/tricoder/engine/loop.py').is_file())
        left=_report('quality-baseline','deepseek-v4-flash',[_trial('quality-baseline','sf-subtract',1,True)])
        right=json.loads(json.dumps(left))
        for payload,d in zip((left,right),definitions):
            payload['fingerprints']=dict(_experiment_fingerprints(d))
        self.assertNotEqual(left['fingerprints']['budgets'],right['fingerprints']['budgets'])
        with self.assertRaises(ComparisonError):compare_report_payloads(left,right)
        with self.assertRaises(ComparisonError):compare_report_payloads(left,right,allowed_variables=frozenset({'budgets'}))
        right=json.loads(json.dumps(left));right['benchmark_version']='quality-v2.1'
        with self.assertRaises(ComparisonError):compare_report_payloads(left,right)

    def test_existing_outside_nonruntime_and_unexpected_source_files_refused(self):
        suite=ROOT/'evals/quality-v2.2'
        for out in (self.root,ROOT/'../outside-delivery',ROOT/'not-runtime-delivery'):
            with self.subTest(out=out),self.assertRaises(ValueError):self.module.prepare(suite,out)
        copied=self.root/'suite';shutil.copytree(suite,copied)
        (copied/'private.db').write_text('synthetic forbidden copy')
        with self.assertRaises(ValueError):self.module.prepare(copied,self.root/'refused')
        self.assertFalse((self.root/'refused').exists())

    def test_link_source_refused(self):
        linked=self.root/'link'
        try:linked.symlink_to(ROOT/'evals/quality-v2.2',target_is_directory=True)
        except OSError:self.skipTest('当前账户不能创建符号链接')
        with self.assertRaises(ValueError):self.module.prepare(linked,self.root/'out')

    def test_same_nine_round_sequence_and_extra_budget_cannot_clear_old_failure(self):
        retry=[call('run_command',command='python -m unittest discover -s tests -t . -v')]
        sequence=reconstructed()+[retry,check('python -m compileall -q .'),finish()]
        for budget,ok,rounds,reason in [(8,False,8,'round_budget_exhausted'),(16,True,9,'completed')]:
            with DeliveryHarness(sequence,max_rounds=budget) as h:
                r=h.run();self.assertEqual(ok,r.ok);self.assertEqual(rounds,r.rounds);self.assertEqual(reason,r.stop_reason.value)
        with DeliveryHarness(reconstructed()+[check('python -m compileall -q .'),finish(),finish()],max_rounds=16) as h:
            r=h.run();self.assertFalse(r.ok);self.assertTrue(r.task_validation.unresolved_check_ids)
            self.assertEqual(9,r.rounds)
            self.assertEqual('verification_blocked',r.stop_reason.value)
        from tricoder.task_cleanup import current_cleanup
        with DeliveryHarness([finish(),finish()],max_rounds=16) as h:
            h.provider.before_response=lambda _:current_cleanup().mark_failed()
            r=h.run();self.assertFalse(r.ok);self.assertEqual('cleanup_failed',r.stop_reason.value)
            self.assertEqual(1,len(h.provider.histories))
