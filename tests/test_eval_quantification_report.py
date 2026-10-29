"""Frozen metadata, backward-compatible records and shared report statistics."""
import json
import unittest
from dataclasses import replace

import test_eval_experiment as experiment_tests
from test_eval_quantification import row, plan_for
from tricoder.evals.models import TrialTimings, ExperimentRunReport
from tricoder.evals.experiment import run_experiment
from tricoder.evals.report import trial_record_as_dict, trial_record_from_dict, experiment_report_as_dict, _render_experiment_markdown


class RecordTests(unittest.TestCase):
    def test_new_fields_round_trip_and_old_defaults(self):
        r=replace(row(),split='holdout',timings=TrialTimings(100,30,20,50))
        raw=trial_record_as_dict(r)
        self.assertEqual('holdout',raw.get('split'))
        self.assertEqual(r,trial_record_from_dict(raw))
        raw.pop('split');raw.pop('timings')
        old=trial_record_from_dict(raw)
        self.assertEqual('unknown',old.split);self.assertIsNone(old.timings)

    def test_invalid_new_fields_rejected(self):
        for field,value in (('split','invalid'),('timings',{'total_ms':True}),
                            ('timings',{'total_ms':3,'agent_ms':1,'verifier_ms':1,'harness_ms':0})):
            raw=trial_record_as_dict(row());raw[field]=value
            with self.assertRaises(ValueError): trial_record_from_dict(raw)

    def test_report_uses_same_formula_and_explains_null(self):
        r=row();report=ExperimentRunReport('run','experiment','suite','2026-10-27T00:00:00+00:00',1,1,True,(r,),trial_plan=plan_for([r]))
        raw=experiment_report_as_dict(report)
        self.assertIn('trial_plan',raw);self.assertIn('quantification',raw)
        self.assertEqual(2,raw['schema_version'])
        text=_render_experiment_markdown(raw)
        for word in ('pass@','pass^','insufficient_repetitions','legacy','provider_reported','billing_completeness'):
            self.assertIn(word,text)
        json.dumps(raw,allow_nan=False)


class FrozenPlanTests(experiment_tests.EvalExperimentTests):
    def quality_definition(self):
        return replace(self.definition,conditions=tuple(replace(c,execution_kind='quality') for c in self.definition.conditions))

    def test_twelve_rows_retain_split_and_condition_isolation(self):
        report=run_experiment(self.quality_definition(),self.run_dir,lambda p,d:self._passed(p))
        self.assertEqual(12,len(report.trial_plan))
        self.assertTrue(all(r.split=='dev' and r.category=='coding' for r in report.trials))
        raw=json.loads((self.run_dir/'plan.json').read_text())
        self.assertTrue(all(p['split']=='dev' for p in raw['planned_trials']))
        q=experiment_report_as_dict(report)['quantification']
        for c in ('baseline','variant'):
            g=q['by_condition'][c]['all_quality']
            self.assertEqual(6,g['coverage']['started'])
            self.assertEqual([3,3],[r['n_started'] for r in g['case_results']])

    def test_errors_and_not_run_have_trusted_metadata(self):
        count=0
        def executor(p,d):
            nonlocal count
            count+=1
            raise RuntimeError('synthetic executor failure')
        report=run_experiment(self.quality_definition(),self.run_dir,executor,cancelled=lambda: count>0)
        self.assertEqual(12,len(report.trial_plan));self.assertEqual('error',report.trials[0].status)
        self.assertTrue(all(r.split=='dev' and r.category=='coding' for r in report.trials))

    def test_explicit_executor_metadata_conflict_is_rejected(self):
        with self.assertRaises(ValueError):
            run_experiment(self.quality_definition(),self.run_dir,
                           lambda p,d:replace(self._passed(p),split='holdout'))


if __name__=='__main__':unittest.main()
