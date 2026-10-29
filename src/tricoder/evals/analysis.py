"""Bounded, explicit-input offline analysis. Never loads configuration or suites."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import TextIO
from uuid import uuid4

from .loader import MAX_EXPERIMENT_TRIALS
from .models import TrialKey, TrialPlanEntry
from .output import _is_link_or_reparse_point, reserve_run_directory, validate_run_directory
from .quantification import quantify_trials, normalize_ks, summarize_counts, USAGE_FIELDS, _condition_summary
from .report import (
    _atomic_write, _require_safe_identifier, _require_positive_count,
    normalize_report_payload, render_quantification_markdown,
    trial_record_from_dict, trial_plan_as_dict,
)

RESULT_LIMIT = 32 * 1024 * 1024
PLAN_LIMIT = 8 * 1024 * 1024
_IDENTITY = ('condition_id','case_id','repetition')
_METADATA = ('execution_kind','category','split')


def _rows(value: object) -> list[dict]:
    if not isinstance(value,list) or len(value)>MAX_EXPERIMENT_TRIALS or any(not isinstance(r,dict) for r in value):
        raise ValueError('invalid rows')
    return value


def _key(row: dict) -> TrialKey:
    return TrialKey(_require_safe_identifier(row.get('condition_id')),
                    _require_safe_identifier(row.get('case_id')),
                    _require_positive_count(row.get('repetition')))


def _index_plan(raw: object, conditions: set[str]) -> dict[TrialKey,dict]:
    result={}
    for p in _rows(raw):
        key=_key(p)
        if key in result or key.condition_id not in conditions: raise ValueError('invalid plan identity')
        for f in _METADATA:
            if f not in p: continue
            if f=='category': _require_safe_identifier(p[f])
            elif p[f] not in (('quality','contract') if f=='execution_kind' else ('dev','holdout','unknown')):
                raise ValueError('invalid plan metadata')
        result[key]={f:p[f] for f in _METADATA if f in p}
    return result


def parse_analysis_input(payload: object, plan_payload: object | None=None):
    """Validate identities before deriving anything; retain unresolved old plans."""
    if not isinstance(payload,dict) or type(payload.get('schema_version')) is not int or payload['schema_version']!=2:
        raise ValueError('unsupported report')
    raw_rows=_rows(payload.get('trials'))
    for raw in raw_rows:
        for channel in ('usage','summary_usage'):
            usage=raw.get(channel)
            if usage is not None:
                if not isinstance(usage,dict): raise ValueError('invalid usage')
                for f in USAGE_FIELDS: summarize_counts((usage.get(f),))
    count=payload.get('planned_trials')
    if type(count) is not int or not 0<=count<=MAX_EXPERIMENT_TRIALS or len(raw_rows)>count:
        raise ValueError('invalid plan count')
    complete=payload.get('complete')
    if complete is not None and type(complete) is not bool: raise ValueError('invalid completeness')
    conditions={}
    for c in _rows(payload.get('conditions')):
        cid=_require_safe_identifier(c.get('id'))
        if cid in conditions or c.get('execution_kind') not in ('quality','contract'): raise ValueError('invalid condition')
        conditions[cid]=c['execution_kind']
    rows=tuple(trial_record_from_dict(r) for r in raw_rows)
    seen=set()
    for r in rows:
        if r.key in seen or r.key.condition_id not in conditions or conditions[r.key.condition_id]!=r.execution_kind:
            raise ValueError('invalid trial identity')
        seen.add(r.key)
    # Empty default trial_plan denotes a pre-extension report, not a proven empty schedule.
    embedded=payload.get('trial_plan')
    if embedded is not None: _rows(embedded)
    embedded_index=_index_plan(embedded,set(conditions)) if embedded else None
    explicit_index=None
    if plan_payload is not None:
        if not isinstance(plan_payload,dict): raise ValueError('invalid plan')
        for f in ('experiment_id','suite_id'):
            if f in plan_payload and plan_payload[f]!=payload.get(f): raise ValueError('plan report conflict')
        if 'schema_version' in plan_payload and (type(plan_payload['schema_version']) is not int or plan_payload['schema_version']!=1):
            raise ValueError('unsupported plan')
        explicit_index=_index_plan(plan_payload.get('planned_trials'),set(conditions))
    if embedded_index is not None and explicit_index is not None:
        if embedded_index.keys()!=explicit_index.keys(): raise ValueError('plan identity conflict')
        for key, left in embedded_index.items():
            right=explicit_index[key]
            if any(left[f]!=right[f] for f in left.keys() & right.keys()): raise ValueError('plan metadata conflict')
    index=embedded_index if embedded_index is not None else explicit_index
    if index is not None:
        if len(index)!=count or not seen<=index.keys(): raise ValueError('plan coverage conflict')
        if embedded_index is not None and explicit_index is not None:
            index={k:explicit_index[k]|v for k,v in embedded_index.items()}
    entries=[]; unresolved=[]
    for key, declared in (index or {}).items():
        same_case=[r for r in rows if r.key.case_id==key.case_id]
        inferred={}
        for f in ('execution_kind','category'):
            values={getattr(r,f) for r in same_case}
            if len(values)>1: raise ValueError('inconsistent historical metadata')
            inferred[f]=next(iter(values),None)
        metadata={**inferred,'split':'unknown',**declared}
        if metadata['execution_kind'] is None:
            unresolved.append(key)
            continue
        # A new plan always declares category; unknown is the only safe old default.
        metadata['category']=metadata['category'] or 'unknown'
        if conditions[key.condition_id]!=metadata['execution_kind']: raise ValueError('plan condition conflict')
        entries.append(TrialPlanEntry(key,**metadata))
    by_key={e.key:e for e in entries}
    enriched=[]
    for r,raw in zip(rows,raw_rows):
        if r.key in by_key:
            p=by_key[r.key]
            if any(f in raw and raw[f]!=getattr(p,f) for f in _METADATA): raise ValueError('row plan metadata conflict')
            r=replace(r,category=p.category,split=p.split)
        enriched.append(r)
    plan=None if index is None else tuple(entries)
    return tuple(enriched),plan,tuple(unresolved)


def analyze_report_payload(payload: object, *, plan_payload: object | None=None,
                           ks: tuple[int,...]=(1,2,3)) -> dict[str,object]:
    ks=normalize_ks(ks)
    if not isinstance(payload,dict) or type(payload.get('schema_version')) is not int:
        raise ValueError('invalid report')
    if payload['schema_version']==1:
        _rows(payload.get('cases'))
        safe=normalize_report_payload(payload)
        if plan_payload is not None: raise ValueError('legacy schema has no repeat identities')
        return {'schema_version':1,'source_schema_version':1,'run_id':safe['run_id'],
                'suite_id':safe['suite_id'],'legacy_case_count':len(safe['trials']),
                'plan_status':'missing_plan','quantification':quantify_trials((),plan=None,ks=ks)}
    rows,plan,unresolved=parse_analysis_input(payload,plan_payload)
    q=quantify_trials(rows,plan=plan,ks=ks,report_complete=None if unresolved else payload.get('complete'))
    if unresolved:
        # Keep the explicit planned denominator, even when old metadata cannot identify missing tasks.
        for condition in sorted({key.condition_id for key in unresolved}):
            block=q['by_condition'].setdefault(condition,_condition_summary((),(),ks,True))
            for group in (block['all_quality'],*block['by_split'].values(),*block['by_category'].values()):
                group['coverage']['incomplete']=True
                group['coverage']['planned']=None  # Missing execution kind prevents a quality denominator.
                for metric in ('artifact_pass_at_k','delivery_pass_at_k','delivery_pass_power_k'):
                    for estimate in group[metric].values():
                        estimate.update(value=None,reason='incomplete_plan',tasks_total=None)
                group['efficiency']['main_reported_tokens_per_delivery_success']={'value':None,'reason':'incomplete_plan'}
    return {'schema_version':1,'source_schema_version':2,
            'run_id':_require_safe_identifier(payload.get('run_id')),
            'experiment_id':_require_safe_identifier(payload.get('experiment_id')),
            'suite_id':_require_safe_identifier(payload.get('suite_id')),
            'planned_trials':payload['planned_trials'],'recorded_trials':len(rows),
            'plan_status':'incomplete_plan' if unresolved else 'missing_plan' if plan is None else 'available',
            'trial_plan':[trial_plan_as_dict(p) for p in plan or ()],
            'unresolved_plan_identities':[dict(zip(_IDENTITY,(k.condition_id,k.case_id,k.repetition))) for k in unresolved],
            'quantification':q}


def _read_input(path: Path, limit: int) -> tuple[object,str]:
    path=path.absolute()
    if any(_is_link_or_reparse_point(p) for p in (path,*path.parents)):
        raise ValueError('input must be normal')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit: raise ValueError('invalid input file')
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno())
        if (info.st_dev,info.st_ino)!=(opened.st_dev,opened.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ValueError('input changed')
        data=stream.read(limit+1)
    if len(data)>limit: raise ValueError('input exceeds limit')
    def reject_constant(_): raise ValueError('invalid JSON number')
    def unique_object(pairs):
        obj={}
        for key,value in pairs:
            if key in obj: raise ValueError('duplicate JSON key')
            obj[key]=value
        return obj
    return json.loads(data.decode('utf8'),parse_constant=reject_constant,object_pairs_hook=unique_object),hashlib.sha256(data).hexdigest()


def run_analyze_command(args: object, *, output: TextIO) -> int:
    try:
        source=Path(getattr(args,'result'))
        payload,result_hash=_read_input(source,RESULT_LIMIT)
        plan_path=getattr(args,'plan',None)
        plan_payload=None;plan_hash=None
        if plan_path is not None: plan_payload,plan_hash=_read_input(Path(plan_path),PLAN_LIMIT)
        raw_ks=getattr(args,'ks',(1,2,3))
        ks=tuple(int(k) if type(k) is str else k for k in raw_ks)
        result=analyze_report_payload(payload,plan_payload=plan_payload,ks=ks)
        result['source_hashes']={'result_sha256':result_hash,'plan_sha256':plan_hash}
        # Re-read only explicit sources, checking that analysis never writes them.
        if _read_input(source,RESULT_LIMIT)[1]!=result_hash: raise ValueError('input changed')
        if plan_path is not None and _read_input(Path(plan_path),PLAN_LIMIT)[1]!=plan_hash: raise ValueError('plan changed')
        directory=reserve_run_directory(Path.cwd(),'analyze-'+uuid4().hex)
        validate_run_directory(Path.cwd(),directory,require_empty=True,create=False)
        json_path=directory/'analysis.json';markdown_path=directory/'analysis.md'
        _atomic_write(json_path,json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        _atomic_write(markdown_path,'# Offline Eval analysis\n\n'+render_quantification_markdown(result['quantification']))
    except Exception:
        output.write('eval_analyze_error=invalid_input\n')
        return 2
    output.write(f'result={json_path}\nreport={markdown_path}\n')
    return 0
