"""Offline input boundaries and strict paired quantification, no models."""
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_eval_compare import _report, _trial
from tricoder.evals.compare import compare_report_payloads, ComparisonError, _comparison_markdown


def payload(condition='one', success=True, repetitions=1):
    raw=_report(condition,'offline',[_trial(condition,'a',i,success) for i in range(1,repetitions+1)])
    raw['trial_plan']=[{k:r[k] for k in ('condition_id','case_id','repetition','execution_kind')}
                       | {'category':'unknown','split':'unknown'} for r in raw['trials']]
    return raw


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tricoder.evals.analysis'),'offline analysis API is missing')
        self.a=importlib.import_module('tricoder.evals.analysis')

    def test_embedded_plan_recomputes_untrusted_scores(self):
        raw=payload(success=False);raw['quantification']={'pass@1':1}
        result=self.a.analyze_report_payload(raw)
        self.assertEqual(0,result['quantification']['by_condition']['one']['all_quality']['delivery_pass_at_k']['1']['value'])
        self.assertNotIn('pass@1',result['quantification'])

    def test_old_plan_unknown_split_and_missing_plan(self):
        raw=payload();plan={'planned_trials':[{k:r[k] for k in ('condition_id','case_id','repetition')} for r in raw.pop('trial_plan')]}
        q=self.a.analyze_report_payload(raw,plan_payload=plan)['quantification']['by_condition']['one']
        self.assertEqual(1,q['by_split']['unknown']['coverage']['planned'])
        self.assertEqual('not_applicable',q['by_split']['dev']['delivery_pass_at_k']['1']['reason'])
        self.assertEqual('missing_plan',self.a.analyze_report_payload(raw)['quantification']['by_condition']['one']['all_quality']['delivery_pass_at_k']['1']['reason'])

    def test_plan_conflicts_and_count_are_rejected(self):
        raw=payload()
        for mutate in (lambda p:p.update(suite_id='other'),lambda p:p.update(experiment_id='other'),
                       lambda p:p['planned_trials'][0].update(split='dev'),
                       lambda p:p['planned_trials'][0].update(condition_id='other'),
                       lambda p:p['planned_trials'].append(p['planned_trials'][0]),
                       lambda p:p['planned_trials'][0].update(repetition=True)):
            plan={'planned_trials':copy.deepcopy(raw['trial_plan'])};mutate(plan)
            with self.subTest(plan=plan),self.assertRaises(ValueError): self.a.analyze_report_payload(raw,plan_payload=plan)
        raw['planned_trials']=2
        with self.assertRaises(ValueError):self.a.analyze_report_payload(raw)

    def test_legacy_absent_case_is_not_removed_from_denominator(self):
        raw=payload();raw.pop('trial_plan');raw['planned_trials']=2
        plan={'planned_trials':[{'condition_id':'one','case_id':case,'repetition':1} for case in ('a','missing')]}
        result=self.a.analyze_report_payload(raw,plan_payload=plan)
        self.assertEqual('incomplete_plan',result['quantification']['by_condition']['one']['all_quality']['delivery_pass_at_k']['1']['reason'])
        self.assertEqual(2,result['planned_trials'])

    def test_missing_all_results_with_explicit_metadata_remains_incomplete(self):
        raw=payload();raw['trials']=[]
        result=self.a.analyze_report_payload(raw)
        g=result['quantification']['by_condition']['one']['all_quality']
        self.assertEqual(1,g['delivery_pass_at_k']['1']['tasks_total'])
        self.assertEqual('incomplete_plan',g['delivery_pass_at_k']['1']['reason'])

    def test_absent_historical_case_only_blocks_its_condition(self):
        raw=payload();raw.pop('trial_plan')
        raw['conditions'].append(dict(raw['conditions'][0],id='two'))
        raw['planned_trials']=2;raw['complete']=False
        plan={'planned_trials':[{'condition_id':'one','case_id':'a','repetition':1},
                                {'condition_id':'two','case_id':'missing','repetition':1}]}
        result=self.a.analyze_report_payload(raw,plan_payload=plan)
        groups=result['quantification']['by_condition']
        self.assertEqual(1,groups['one']['all_quality']['delivery_pass_at_k']['1']['value'])
        self.assertEqual('incomplete_plan',groups['two']['all_quality']['delivery_pass_at_k']['1']['reason'])
        self.assertIsNone(groups['two']['all_quality']['delivery_pass_at_k']['1']['tasks_total'])

    def test_cli_rejects_non_numeric_k_with_fixed_error(self):
        from tricoder.cli import main
        out=io.StringIO()
        self.assertEqual(2,main(['eval-analyze','--result','missing.json','--ks','no','--no-color'],output=out))
        self.assertEqual('eval_analyze_error=invalid_input\n',out.getvalue())

    def test_schema_one_does_not_invent_trials_or_dimensions(self):
        raw={'schema_version':1,'run_id':'old','suite_id':'old','cases':[{'case_id':'a','status':'passed','usage':{}}]}
        result=self.a.analyze_report_payload(raw)
        self.assertEqual(1,result['source_schema_version'])
        self.assertFalse(result['quantification']['by_condition'])
        self.assertNotIn('trials',result)
        for schema in (True,0,3,'2'):
            with self.assertRaises(ValueError):self.a.analyze_report_payload(dict(raw,schema_version=schema))

    def test_input_limits_and_numeric_validation(self):
        for mutate in (lambda p:p.update(trials=p['trials']*1001),
                       lambda p:p['trials'][0]['usage'].update(input_tokens=True),
                       lambda p:p['trials'][0]['usage'].update(output_tokens=float('nan')),
                       lambda p:p['trials'][0].update(condition_id='not-declared'),
                       lambda p:p['trials'][0].update(repetition=0)):
            raw=payload();mutate(raw)
            with self.assertRaises(ValueError):self.a.analyze_report_payload(raw)
        for ks in ((),(True,),(101,),(-1,)):
            with self.assertRaises(ValueError):self.a.analyze_report_payload(payload(),ks=ks)

    def test_cli_is_offline_immutable_and_writes_exclusive_outputs(self):
        from tricoder.cli import main
        with tempfile.TemporaryDirectory() as d:
            old=Path.cwd();os.chdir(d)
            try:
                source=Path('result.json');source.write_text(json.dumps(payload(success=False)),encoding='utf8')
                before=hashlib.sha256(source.read_bytes()).hexdigest()
                with (patch('tricoder.cli.load_config',side_effect=AssertionError('config forbidden')),
                      patch('tricoder.evals.service.load_config',side_effect=AssertionError('config forbidden'))):
                    for _ in range(2):
                        out=io.StringIO()
                        code=main(['eval-analyze','--result',str(source),'--no-color'],output=out,
                                  provider_factory=lambda *a,**kw:self.fail('provider forbidden'),
                                  session_store_factory=lambda *a,**kw:self.fail('database forbidden'))
                        self.assertEqual(0,code,out.getvalue())
                self.assertEqual(before,hashlib.sha256(source.read_bytes()).hexdigest())
                reports=list(Path('runtime/evals').glob('analyze-*/analysis.json'))
                self.assertEqual(2,len(reports))
                for p in reports:self.assertEqual(before,json.loads(p.read_text())['source_hashes']['result_sha256'])
            finally:os.chdir(old)

    def test_cli_rejects_directory_reparse_oversize_with_fixed_error(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'result.json';path.write_text(json.dumps(payload()))
            for target in (Path(d),path):
                output=io.StringIO()
                with patch.object(self.a,'_is_link_or_reparse_point',return_value=target==path):
                    self.assertEqual(2,self.a.run_analyze_command(SimpleNamespace(result=target,plan=None,ks=[1]),output=output))
                self.assertEqual('eval_analyze_error=invalid_input\n',output.getvalue())
            with patch.object(self.a,'RESULT_LIMIT',1):
                output=io.StringIO()
                self.assertEqual(2,self.a.run_analyze_command(SimpleNamespace(result=path,plan=None,ks=[1]),output=output))


class ComparisonQuantificationTests(unittest.TestCase):
    def test_comparison_markdown_displays_new_paired_metrics(self):
        result=compare_report_payloads(payload('base',False,3),payload('candidate',True,3))
        text=_comparison_markdown(result)
        self.assertIn('delivery_pass_at_k',text)
        self.assertIn('100.0',text)
        self.assertIn('provider_reported',text)

    def test_invalid_cache_blocks_token_delta(self):
        left=payload('base');right=payload('candidate')
        right['trials'][0]['usage']['cached_tokens']=4
        q=compare_report_payloads(left,right)['quantification_comparison']
        self.assertIsNone(q['deltas']['main']['reported_total_tokens']['value'])
        self.assertEqual('invalid_usage',q['deltas']['main']['reported_total_tokens']['reason'])

    def test_paired_different_condition_names(self):
        left=payload('base',False,3);right=payload('candidate',True,3)
        result=compare_report_payloads(left,right)
        self.assertIn('quantification_comparison',result)
        q=result['quantification_comparison']
        self.assertIsNone(q['reason'])
        self.assertEqual(100,q['deltas']['delivery_pass_at_k']['3']['value'])
        self.assertEqual(100,q['deltas']['delivery_pass_power_k']['3']['value'])

    def test_unpaired_and_old_plans_never_gain_new_deltas(self):
        left=payload('base');right=payload('candidate',False,2)
        result=compare_report_payloads(left,right)
        self.assertIn('quantification_comparison',result)
        q=result['quantification_comparison'];self.assertEqual('unpaired_plan',q['reason'])
        self.assertIsNone(q['deltas']['delivery_pass_at_k']['1']['value'])
        right=payload('candidate');left.pop('trial_plan')
        q=compare_report_payloads(left,right)['quantification_comparison']
        self.assertEqual('missing_plan',q['reason'])
        self.assertIsNone(q['deltas']['main']['reported_total_tokens']['value'])

    def test_wrong_condition_identity_rejected_even_without_plan(self):
        left=payload('base');right=payload('candidate');left.pop('trial_plan');left['trials'][0]['condition_id']='forged'
        with self.assertRaises(ValueError):compare_report_payloads(left,right)

    def test_metadata_mismatch_not_paired_and_fingerprints_stay_strict(self):
        left=payload('base');right=payload('candidate')
        right['trial_plan'][0]['split']='dev';right['trials'][0]['split']='dev'
        q=compare_report_payloads(left,right).get('quantification_comparison')
        self.assertIsNotNone(q);self.assertEqual('unpaired_plan',q['reason'])
        for key in ('suite','verifier','budgets'):
            bad=copy.deepcopy(right);bad['fingerprints'][key]='f'*64
            with self.assertRaises(ComparisonError):compare_report_payloads(left,bad)


if __name__=='__main__':unittest.main()
