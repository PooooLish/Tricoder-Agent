"""Pure whole-task estimators; no I/O, configuration or Provider dependencies."""
from __future__ import annotations

from math import comb, ceil
from statistics import median

from .models import TrialRecord, TrialPlanEntry

METHOD = "tricoder-eval-quantification-v1"
USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cache_miss_tokens")
NOT_RUN = {"not_run_budget", "not_run_cancelled"}
INCOMPLETE = NOT_RUN | {"cancelled"}


def normalize_ks(ks: tuple[int, ...]) -> tuple[int, ...]:
    if not ks or any(type(k) is not int or not 1 <= k <= 100 for k in ks):
        raise ValueError("invalid k")
    return tuple(sorted(set(ks)))


def _counts(n: int, c: int, k: int) -> None:
    normalize_ks((k,))
    if type(n) is not int or type(c) is not int or not 0 <= c <= n or n < k:
        raise ValueError("invalid sample counts")


def estimate_pass_at_k(n: int, c: int, k: int) -> float:
    _counts(n, c, k)
    return 1 - (comb(n-c, k) if n-c >= k else 0) / comb(n, k)


def estimate_pass_power_k(n: int, c: int, k: int) -> float:
    _counts(n, c, k)
    return (comb(c, k) if c >= k else 0) / comb(n, k)


def summarize_counts(values: tuple[int | None, ...]) -> dict[str, object]:
    if any(v is not None and (type(v) is not int or v < 0) for v in values):
        raise ValueError("invalid observed count")
    known = sorted(v for v in values if v is not None)
    n = len(known)
    return {
        "observed": n, "missing": len(values)-n, "population": "observed_subset",
        "observed_sum": sum(known) if n else None,
        "strict_sum": sum(known) if n and n == len(values) else None,
        "mean": sum(known)/n if n else None, "median": median(known) if n else None,
        "p90": known[ceil(.9*n)-1] if n else None,
        "p95": known[ceil(.95*n)-1] if n else None, "max": known[-1] if n else None,
    }


def delivery_outcome(row: TrialRecord) -> bool | None:
    """Quality four-dimensional AND, with conservative infrastructure failures."""
    d = row.dimensions
    values = (d.artifact_correct, d.agent_completed, d.scope_compliant, d.cleanup_confirmed)
    if row.status == "error" or False in values:
        return False
    return None if None in values else True


def artifact_outcome(row: TrialRecord) -> bool | None:
    value = row.dimensions.artifact_correct
    return False if value is None and row.status == "error" else value


def _usage(row: TrialRecord, summary: bool) -> dict[str, int | None]:
    usage = row.summary_usage if summary else row.usage
    values = {f: getattr(usage, f, None) for f in USAGE_FIELDS}
    if summary and row.summary_count == 0 and row.summary_triggered is False and all(v is None for v in values.values()):
        values = dict.fromkeys(USAGE_FIELDS, 0)
    i, o = values['input_tokens'], values['output_tokens']
    values['reported_total_tokens'] = i+o if i is not None and o is not None else None
    return values


def _channel(rows: tuple[TrialRecord, ...], summary: bool) -> dict[str, object]:
    values = [_usage(r, summary) for r in rows]
    result = {f: summarize_counts(tuple(v[f] for v in values))
              for f in (*USAGE_FIELDS, 'reported_total_tokens')}
    pairs = [(v['input_tokens'], v['cached_tokens']) for v in values]
    invalid = sum(i is not None and c is not None and c > i for i,c in pairs)
    known = [(i,c) for i,c in pairs if i is not None and c is not None and c <= i]
    total_input = sum(i for i,c in known)
    ratio = sum(c for i,c in known)/total_input if total_input else None
    result['cache_ratio'] = {
        'observed_ratio': ratio, 'strict_ratio': ratio if len(known)==len(rows) and not invalid else None,
        'observed': len(known), 'missing': len(rows)-len(known)-invalid, 'invalid': invalid,
    }
    return result


def _subset(rows: tuple[TrialRecord, ...]) -> dict[str, object]:
    result = {'trials': len(rows), 'main': _channel(rows, False), 'summary': _channel(rows, True)}
    for f in ('duration_ms', 'rounds', 'tool_calls', 'retries'):
        result['legacy_eval_duration_ms' if f == 'duration_ms' else f] = summarize_counts(tuple(getattr(r,f) for r in rows))
    result['timings'] = {f: summarize_counts(tuple(getattr(r.timings,f,None) for r in rows))
                         for f in ('total_ms','agent_ms','verifier_ms','harness_ms')}
    return result


def _efficiency(rows: tuple[TrialRecord, ...], incomplete: bool) -> dict[str, object]:
    started = tuple(r for r in rows if r.status not in NOT_RUN)
    success = tuple(r for r in started if delivery_outcome(r) is True)
    unknown = tuple(r for r in started if delivery_outcome(r) is None)
    result = {name: _subset(subset) for name,subset in (
        ('all_started',started), ('successful',success),
        ('unsuccessful',tuple(r for r in started if delivery_outcome(r) is False)),
        ('unknown_outcome',unknown))}
    main = result['all_started']['main']
    total = main['reported_total_tokens']['strict_sum']
    reason = ('incomplete_plan' if incomplete else 'unknown_outcome' if unknown
              else 'invalid_usage' if main['cache_ratio']['invalid']
              else 'missing_usage' if total is None else 'no_success' if not success else None)
    result['main_reported_tokens_per_delivery_success'] = {
        'value': total/len(success) if reason is None else None, 'reason': reason}
    return result


def _validate(rows: tuple[TrialRecord,...], plan: tuple[TrialPlanEntry,...] | None) -> None:
    keys = set()
    metadata = {}
    for r in rows:
        TrialPlanEntry(r.key,r.execution_kind,r.category,r.split)
        if r.key in keys: raise ValueError('duplicate trial')
        keys.add(r.key)
        if r.status not in {'passed','failed','error',*INCOMPLETE}: raise ValueError('invalid status')
        d = r.dimensions
        for f in d.__dataclass_fields__:
            v=getattr(d,f)
            if v is not None and type(v) is not bool: raise ValueError('invalid dimension')
        if r.execution_kind == 'quality' and r.status == 'passed' and any(
            getattr(d,f) is False for f in ('artifact_correct','agent_completed','scope_compliant','cleanup_confirmed')):
            raise ValueError('contradictory passed quality trial')
        for f in ('duration_ms','rounds','tool_calls','retries','summary_count'):
            summarize_counts((getattr(r,f),))
        if r.summary_triggered is not None and type(r.summary_triggered) is not bool:
            raise ValueError('invalid summary flag')
        for usage in (r.usage,r.summary_usage):
            for f in USAGE_FIELDS: summarize_counts((getattr(usage,f,None),))
    if plan is not None:
        planned = {}
        for p in plan:
            if not isinstance(p,TrialPlanEntry) or p.key in planned: raise ValueError('invalid plan')
            planned[p.key]=p
        for r in rows:
            if r.key not in planned or (r.execution_kind,r.category,r.split) != (
                planned[r.key].execution_kind,planned[r.key].category,planned[r.key].split):
                raise ValueError('trial plan conflict')
    for item in (*rows,*(plan or ())):
        key=(item.key.condition_id,item.key.case_id)
        value=(item.execution_kind,item.category,item.split)
        if key in metadata and metadata[key]!=value: raise ValueError('case metadata conflict')
        metadata[key]=value


def _group(rows, plan, ks, incomplete):
    rows=tuple(r for r in rows if r.execution_kind=='quality')
    plan=None if plan is None else tuple(p for p in plan if p.execution_kind=='quality')
    ids=sorted({r.key.case_id for r in rows} | {p.key.case_id for p in plan or ()})
    cases=[]; histogram={'artifact':{},'delivery':{}}
    for case in ids:
        rs=tuple(r for r in rows if r.key.case_id==case)
        started=tuple(r for r in rs if r.status not in NOT_RUN)
        nplan=sum(p.key.case_id==case for p in plan) if plan is not None else None
        a=[artifact_outcome(r) for r in started]; d=[delivery_outcome(r) for r in started]
        item={'case_id':case,'n_planned':nplan,'n_started':len(started),
              'c_artifact':sum(v is True for v in a),'c_delivery':sum(v is True for v in d),
              'unknown_artifact':sum(r.dimensions.artifact_correct is None for r in started),
              'unknown_delivery':sum(any(getattr(r.dimensions,f) is None for f in (
                  'artifact_correct','agent_completed','scope_compliant','cleanup_confirmed')) for r in started),
              'unresolved_artifact':sum(v is None for v in a), 'unresolved_delivery':sum(v is None for v in d)}
        cases.append(item)
        n=len(started)
        for name, values in (('artifact',a),('delivery',d)):
            bucket=histogram[name].setdefault(str(n),{**{f'{c}/{n}':0 for c in range(n+1)},'unknown':0})
            key='unknown' if None in values or (nplan is not None and nplan != n) or any(r.status in INCOMPLETE for r in rs) else f'{sum(v is True for v in values)}/{n}'
            bucket[key]+=1
    result={'coverage':{'planned':len(plan) if plan is not None else None,'recorded':len(rows),
                        'started':sum(r.status not in NOT_RUN for r in rows),
                        'infrastructure_errors':sum(r.status=='error' for r in rows),
                        'missing':len(plan)-len(rows) if plan is not None else None,
                        'incomplete':incomplete},'case_results':cases,'success_histograms':histogram,
            'efficiency':_efficiency(rows,incomplete)}
    for metric, outcome, estimator in (
        ('artifact_pass_at_k','artifact',estimate_pass_at_k),
        ('delivery_pass_at_k','delivery',estimate_pass_at_k),
        ('delivery_pass_power_k','delivery',estimate_pass_power_k)):
        result[metric]={}
        for k in ks:
            eligible=[c for c in cases if c['n_started']>=k and not c['unresolved_'+outcome]
                      and (c['n_planned'] is None or c['n_started']==c['n_planned'])]
            reason=('not_applicable' if not cases else 'missing_plan' if plan is None
                    else 'incomplete_plan' if incomplete
                    else 'unknown_outcome' if any(c['unresolved_'+outcome] for c in cases)
                    else 'insufficient_repetitions' if len(eligible)!=len(cases) else None)
            result[metric][str(k)]={'value':sum(estimator(c['n_started'],c['c_'+outcome],k) for c in cases)/len(cases) if reason is None else None,
                                   'tasks_total':len(cases),'tasks_eligible':len(eligible),'reason':reason}
    return result


def _condition_summary(rows, plan, ks, incomplete):
    group=lambda rr,pp: _group(rr,pp,ks,incomplete)
    return {'all_quality':group(rows,plan),
            'by_split':{s:group(tuple(r for r in rows if r.split==s),None if plan is None else tuple(p for p in plan if p.split==s)) for s in ('dev','holdout','unknown')},
            'by_category':{s:group(tuple(r for r in rows if r.category==s),None if plan is None else tuple(p for p in plan if p.category==s))
                           for s in sorted({r.category for r in rows}|{p.category for p in plan or ()})}}


def quantify_trials(trials: tuple[TrialRecord,...], *, plan: tuple[TrialPlanEntry,...] | None,
                    ks: tuple[int,...]=(1,2,3), report_complete: bool | None=None) -> dict[str,object]:
    ks=normalize_ks(ks); _validate(trials,plan)
    if report_complete is not None and type(report_complete) is not bool: raise ValueError('invalid completeness')
    conditions=sorted({r.key.condition_id for r in trials}|{p.key.condition_id for p in plan or ()})
    incomplete={c:False for c in conditions}
    for c in conditions:
        rs=tuple(r for r in trials if r.key.condition_id==c)
        ps=tuple(p for p in plan or () if p.key.condition_id==c)
        incomplete[c]=any(r.status in INCOMPLETE for r in rs) or (plan is not None and len(rs)!=len(ps))
    if report_complete is False and not any(incomplete.values()): incomplete=dict.fromkeys(conditions,True)
    result={}
    for c in conditions:
        rs=tuple(r for r in trials if r.key.condition_id==c)
        ps=None if plan is None else tuple(p for p in plan if p.key.condition_id==c)
        result[c]=_condition_summary(rs,ps,ks,incomplete[c])
    return {'schema_version':1,'method':METHOD,'ks':list(ks),'token_accounting':'provider_reported',
            'billing_completeness':'unknown','error_policy':'conservative_end_to_end_failure',
            'by_condition':result}
