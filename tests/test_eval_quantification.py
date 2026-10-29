"""Hand-calculated oracles for whole-task, condition-isolated estimates."""
import importlib
import unittest
from dataclasses import replace

from tricoder.evals import models
from tricoder.models import TokenUsage


def row(case='a', rep=1, condition='one', success=True, **kw):
    return models.TrialRecord(
        models.TrialKey(condition, case, rep), 'quality',
        'passed' if success else 'failed',
        models.TrialDimensions(success, success, True, True), **kw)


def plan_for(rows):
    return tuple(models.TrialPlanEntry(r.key, r.execution_kind, r.category, r.split)
                 for r in rows)


class QuantificationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tricoder.evals.quantification'),
                             'pure whole-task quantification API is missing')
        self.q = importlib.import_module('tricoder.evals.quantification')

    def group(self, rows, plan=True, **kw):
        return self.q.quantify_trials(tuple(rows), plan=plan_for(rows) if plan is True else plan,
                                     **kw)['by_condition']['one']['all_quality']

    def test_hand_calculated_combinations(self):
        for k, at, power in ((1, 2/3, 2/3), (2, 1, 1/3), (3, 1, 0)):
            self.assertAlmostEqual(at, self.q.estimate_pass_at_k(3, 2, k))
            self.assertAlmostEqual(power, self.q.estimate_pass_power_k(3, 2, k))
        for c in (0, 3):
            for k in (1, 2, 3):
                self.assertEqual(c/3, self.q.estimate_pass_at_k(3, c, k))
                self.assertEqual(c/3, self.q.estimate_pass_power_k(3, c, k))

    def test_invalid_counts_and_k(self):
        for args in ((True, 1, 1), (3, True, 1), (3, 2, True), (0, 0, 1),
                     (3, 4, 1), (3, -1, 1), (3, 1, 0), (3, 1, 101),
                     (3, 1, 4), (3, 1, 1.5)):
            for fn in (self.q.estimate_pass_at_k, self.q.estimate_pass_power_k):
                with self.subTest(args=args), self.assertRaises(ValueError): fn(*args)

    def test_macro_average_not_trial_average(self):
        rows = [row(case, i, success=(case == 'a' and i < 3))
                for case in ('a', 'b') for i in (1, 2, 3)]
        g = self.group(rows)
        for k, expected in ((1, 1/3), (2, 1/2), (3, 1/2)):
            self.assertAlmostEqual(expected, g['delivery_pass_at_k'][str(k)]['value'])
        self.assertAlmostEqual(1/6, g['delivery_pass_power_k']['2']['value'])
        self.assertEqual({'0/3': 1, '1/3': 0, '2/3': 1, '3/3': 0, 'unknown': 0},
                         g['success_histograms']['delivery']['3'])

    def test_unequal_repetitions_do_not_drop_tasks(self):
        g = self.group([row(rep=i) for i in (1, 2, 3)] + [row('b', success=False)])
        self.assertEqual(.5, g['delivery_pass_at_k']['1']['value'])
        self.assertEqual('insufficient_repetitions', g['delivery_pass_at_k']['2']['reason'])
        self.assertEqual(1, g['delivery_pass_at_k']['2']['tasks_eligible'])

    def test_conditions_and_contract_are_not_samples(self):
        rows = (row(), row(condition='two', success=False),
                replace(row('contract'), execution_kind='contract', dimensions=models.TrialDimensions(False, False)))
        q = self.q.quantify_trials(rows, plan=plan_for(rows))
        for c, v in (('one', 1), ('two', 0)):
            g = q['by_condition'][c]['all_quality']
            self.assertEqual(v, g['delivery_pass_at_k']['1']['value'])
            self.assertEqual(1, len(g['case_results']))
        self.assertEqual('not_applicable', self.group([replace(rows[2], key=models.TrialKey('one','contract',1))])['artifact_pass_at_k']['1']['reason'])

    def test_missing_plan_and_missing_rows(self):
        rows = (row(), row(rep=2))
        self.assertEqual('missing_plan', self.group(rows, plan=None)['artifact_pass_at_k']['1']['reason'])
        g = self.group(rows[:1], plan=plan_for(rows))
        self.assertEqual('incomplete_plan', g['artifact_pass_at_k']['1']['reason'])
        self.assertEqual(2, g['case_results'][0]['n_planned'])
        for status in ('not_run_budget', 'not_run_cancelled', 'cancelled'):
            with self.subTest(status=status):
                g = self.group([replace(row(), status=status)])
                self.assertEqual('incomplete_plan', g['delivery_pass_at_k']['1']['reason'])

    def test_condition_incompleteness_is_local(self):
        rows = (row(), replace(row(condition='two'), status='not_run_budget'))
        q = self.q.quantify_trials(rows, plan=plan_for(rows), report_complete=False)
        self.assertEqual(1, q['by_condition']['one']['all_quality']['delivery_pass_at_k']['1']['value'])
        self.assertEqual('incomplete_plan', self.group([row()], report_complete=False)['delivery_pass_at_k']['1']['reason'])

    def test_unknown_and_error_are_distinct(self):
        unknown = replace(row(rep=3), status='failed', dimensions=models.TrialDimensions())
        rows = [row(rep=1), row(rep=2), unknown]
        self.assertEqual('unknown_outcome', self.group(rows)['delivery_pass_at_k']['1']['reason'])
        rows[-1] = replace(unknown, status='error')
        g = self.group(rows)
        self.assertAlmostEqual(2/3, g['delivery_pass_at_k']['1']['value'])
        self.assertEqual(1, g['coverage']['infrastructure_errors'])
        self.assertEqual(1, g['case_results'][0]['unknown_artifact'])

    def test_four_dimensions_not_status(self):
        r = replace(row(), status='failed', dimensions=models.TrialDimensions(True, False, True, True))
        g = self.group([r]); self.assertEqual(1, g['artifact_pass_at_k']['1']['value'])
        self.assertEqual(0, g['delivery_pass_at_k']['1']['value'])
        self.assertEqual(1, self.group([replace(row(), status='failed')])['delivery_pass_at_k']['1']['value'])
        with self.assertRaises(ValueError): self.group([replace(r, status='passed')])
        r = replace(row(), dimensions=models.TrialDimensions(None, True, True, True))
        self.assertEqual('unknown_outcome', self.group([r])['delivery_pass_at_k']['1']['reason'])

    def test_identity_and_metadata_conflicts_rejected(self):
        r = row(); p = plan_for([r])
        for rows, plan in (((r,r),p), ((replace(r,key=models.TrialKey('two','a',1)),),p),
                           ((r,),p+p), ((replace(r,category='other'),),p),
                           ((replace(r,key=models.TrialKey('one','a',True)),),None)):
            with self.assertRaises(ValueError): self.q.quantify_trials(rows, plan=plan)

    def test_counts_distributions_and_partial_missing(self):
        s = self.q.summarize_counts((12,24))
        for field, value in {'strict_sum':36,'mean':18,'median':18,'p90':24,'p95':24,'max':24}.items():
            self.assertEqual(value,s[field])
        s = self.q.summarize_counts((12,None))
        self.assertEqual(12,s['observed_sum']); self.assertIsNone(s['strict_sum'])
        self.assertEqual(1,s['missing'])
        self.assertIsNone(self.q.summarize_counts(())['observed_sum'])
        for bad in (True,-1,float('nan'),float('inf'),1.5):
            with self.assertRaises(ValueError): self.q.summarize_counts((bad,))

    def test_token_failure_amortization_cache_and_channels(self):
        rows = [row(usage=TokenUsage(10,2,8), summary_count=0,summary_triggered=False),
                row(rep=2, success=False, usage=TokenUsage(20,4,10),summary_count=1)]
        e = self.group(rows)['efficiency']
        self.assertEqual(36,e['main_reported_tokens_per_delivery_success']['value'])
        self.assertEqual(.6,e['all_started']['main']['cache_ratio']['strict_ratio'])
        self.assertEqual(36,e['all_started']['main']['reported_total_tokens']['strict_sum'])
        self.assertEqual(0,e['successful']['summary']['reported_total_tokens']['strict_sum'])
        self.assertIsNone(e['all_started']['summary']['reported_total_tokens']['strict_sum'])
        rows[1] = replace(rows[1],usage=TokenUsage(20,None,10))
        self.assertEqual('missing_usage',self.group(rows)['efficiency']['main_reported_tokens_per_delivery_success']['reason'])

    def test_unknown_denominator_and_amortization_precedence(self):
        r = row(usage=TokenUsage(10,2))
        unknown = replace(r,key=models.TrialKey('one','a',2),status='failed',dimensions=models.TrialDimensions())
        e = self.group([r,unknown])['efficiency']
        self.assertEqual(2,e['all_started']['trials']); self.assertEqual(1,e['unknown_outcome']['trials'])
        self.assertEqual('unknown_outcome',e['main_reported_tokens_per_delivery_success']['reason'])
        self.assertEqual('no_success',self.group([replace(row(success=False),usage=r.usage)])['efficiency']['main_reported_tokens_per_delivery_success']['reason'])

    def test_invalid_cache_not_clamped(self):
        e = self.group([row(usage=TokenUsage(10,2,11))])['efficiency']
        self.assertEqual(1,e['all_started']['main']['cache_ratio']['invalid'])
        self.assertIsNone(e['all_started']['main']['cache_ratio']['strict_ratio'])
        self.assertIsNone(e['main_reported_tokens_per_delivery_success']['value'])
        e = self.group([row(usage=TokenUsage(0,0,0))])['efficiency']
        self.assertIsNone(e['all_started']['main']['cache_ratio']['strict_ratio'])

    def test_observed_cache_ratio_does_not_claim_complete_coverage(self):
        e=self.group([row(usage=TokenUsage(10,2,8)),row(rep=2,usage=TokenUsage(20,4))])['efficiency']
        ratio=e['all_started']['main']['cache_ratio']
        self.assertEqual(.8,ratio['observed_ratio']);self.assertIsNone(ratio['strict_ratio'])
        self.assertEqual(1,ratio['observed']);self.assertEqual(1,ratio['missing'])

    def test_error_preserves_proven_artifact_but_not_delivery(self):
        g=self.group([replace(row(usage=None),status='error')])
        self.assertEqual(1,g['artifact_pass_at_k']['1']['value'])
        self.assertEqual(0,g['delivery_pass_at_k']['1']['value'])
        self.assertEqual('missing_usage',g['efficiency']['main_reported_tokens_per_delivery_success']['reason'])

    def test_false_dimension_resolves_other_unknowns(self):
        r=replace(row(),status='failed',dimensions=models.TrialDimensions(None,False,True,True))
        g=self.group([r])
        self.assertEqual(0,g['delivery_pass_at_k']['1']['value'])
        self.assertEqual('unknown_outcome',g['artifact_pass_at_k']['1']['reason'])
        self.assertEqual(1,g['efficiency']['unsuccessful']['trials'])

    def test_ks_sorted_deduplicated_and_summary_zero_requires_all_evidence(self):
        r=row(summary_count=0,summary_triggered=None)
        q=self.q.quantify_trials((r,),plan=plan_for([r]),ks=(3,1,1,2))
        self.assertEqual([1,2,3],q['ks'])
        s=q['by_condition']['one']['all_quality']['efficiency']['all_started']['summary']
        self.assertIsNone(s['reported_total_tokens']['strict_sum'])
        r=replace(r,summary_triggered=False,summary_usage=TokenUsage(4,None))
        s=self.group([r])['efficiency']['all_started']['summary']
        self.assertEqual(4,s['input_tokens']['strict_sum']);self.assertIsNone(s['reported_total_tokens']['strict_sum'])

    def test_all_missing_results_keep_every_planned_case(self):
        q=self.q.quantify_trials((),plan=plan_for([row(),row('b')]))
        g=q['by_condition']['one']['all_quality']
        self.assertEqual(2,g['delivery_pass_at_k']['1']['tasks_total'])
        self.assertEqual('incomplete_plan',g['delivery_pass_at_k']['1']['reason'])
        self.assertEqual(0,g['coverage']['started'])

    def test_group_metadata_must_be_consistent_across_repetitions(self):
        rows=(row(category='first'),row(rep=2,category='second'))
        with self.assertRaises(ValueError): self.q.quantify_trials(rows,plan=plan_for(rows))

    def test_new_types_validate_without_legacy_invention(self):
        self.assertEqual('unknown',row().split); self.assertIsNone(row().timings)
        self.assertEqual(100,models.TrialTimings(100,30,20,50).total_ms)
        for args in ((100,30,20,49),(True,0,0,1),(-1,0,0,-1)):
            with self.assertRaises(ValueError): models.TrialTimings(*args)
        for split in ('secret/path','test',None):
            with self.assertRaises(ValueError): models.TrialPlanEntry(row().key,'quality','coding',split)

    def test_synthetic_ninety_independent_whole_task_samples(self):
        rows=tuple(row(f'case-{c}',i,success=i<3) for c in range(30) for i in (1,2,3))
        g=self.group(rows)
        self.assertEqual(30,len(g['case_results'])); self.assertEqual(90,g['coverage']['started'])
        self.assertEqual(1,g['delivery_pass_at_k']['3']['value'])
        self.assertEqual(0,g['delivery_pass_power_k']['3']['value'])


if __name__ == '__main__': unittest.main()
