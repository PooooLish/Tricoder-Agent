"""Guidance-only migration with real Runtime/public/hidden scoring receipts."""
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import test_eval_quality_v3_verifiers as variants
import test_eval_quality_v3_review_fixes as fixes
from delivery_helpers import QueueProvider, call, check, finish
from quality_v3_support import ROOT, SUITE as OLD, REFERENCES, cases, isolated
from tricoder.evals.loader import load_suite, load_experiment
from tricoder.evals.experiment import plan_trials
from tricoder.evals.service import _execute_case, _ExecutionObservation
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig

SUITE=ROOT/'evals/quality-v3.1'
COMMAND='python -m unittest discover -v'


def digest_tree(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


class GuidanceSuiteTests(unittest.TestCase):
    def test_identity_split_budget_and_exact_migration_allowlist(self):
        self.assertTrue((SUITE/'suite.toml').is_file(),'V3.1 missing')
        suite=load_suite(SUITE)
        self.assertEqual(('quality-v3-1','quality-v3.1'),(suite.id,suite.benchmark_version))
        self.assertEqual({'quality':30},dict(Counter(c.execution_kind for c in suite.cases)))
        self.assertEqual({'dev':22,'holdout':8},dict(Counter(c.split for c in suite.cases)))
        before=digest_tree(OLD); after=digest_tree(SUITE)
        self.assertEqual(before,json.loads((SUITE/'source-v3-sha256.json').read_text('utf8')))
        expected={'suite.toml','README.md','migration.md'} | {f'experiment-quality-{n}.toml' for n in ('baseline','repeat3','dev','holdout')}
        expected|={f'cases/{c.id}/workspace/README.md' for c in suite.cases}
        self.assertEqual(expected,{p for p in before if before[p]!=after.get(p)})
        self.assertEqual({'source-v3-sha256.json'},set(after)-set(before))
        for c in suite.cases:
            self.assertEqual((16,16000),(c.max_rounds,c.max_context_chars))
            text=(c.source_dir/'workspace/README.md').read_text('utf8')
            self.assertIn('`'+COMMAND+'`',text)
            self.assertNotIn('discover -s tests',text)
            self.assertIn('pending',text)
            self.assertIn('纯确认',text)

    def test_four_plans_keep_exact_keys_but_distinct_version_identity(self):
        for name,count in (('baseline',30),('repeat3',90),('dev',22),('holdout',8)):
            with self.subTest(name=name):
                old=load_experiment(OLD/f'experiment-quality-{name}.toml')
                new=load_experiment(SUITE/f'experiment-quality-{name}.toml')
                self.assertEqual('quality-v3-1-'+name,new.id)
                a,b=plan_trials(old),plan_trials(new)
                self.assertEqual(count,len(b))
                self.assertEqual([t.key for t in a],[t.key for t in b])
                self.assertEqual(old.conditions,new.conditions)
                for field in ('repetitions','split','max_trials','time_budget_seconds','seed'):
                    self.assertEqual(getattr(old,field),getattr(new,field))


def runtime_test(case_id):
    def test(self):
        case=cases(SUITE)[case_id]; observation=_ExecutionObservation(); receipts=[]
        seen_calls=set()
        def execute(c,workspace,audit):
            expected=dict(REFERENCES[c.id]['correct'])
            actions=[]; current={p:(workspace/p).read_text('utf8') for p in expected}
            if c.id=='mm-partial-revision':
                phases=[{'formatter.py':'def render(text,prefix):\n    return prefix+text.strip()\n'},expected]
            else:
                phases=([None]*(len(c.steps)-1)+[expected]) if c.steps else [expected]
            for files in phases:
                if files is not None:
                    # Public and hidden sources are never written by this executor.
                    actions.append([call('read_file',path=p) for p in files])
                    changes=[call('edit_file',path=p,old_text=current[p],new_text=text)
                             for p,text in files.items() if current[p]!=text]
                    if changes: actions.append(changes)
                    current.update(files)
                    actions.append(check(COMMAND))
                actions.append(finish())
            provider=QueueProvider(actions)
            config=AppConfig(workspace=workspace,provider=ProviderConfig('deepseek','offline','https://example.invalid','offline'),
                audit_dir=audit.parent,plan_enabled=False,memory=MemoryConfig(compaction='off',persistence='off'))
            result=_execute_case(c,workspace,audit,config,lambda *_:provider,observation=observation)
            self.assertTrue(result.ok,result)
            self.assertFalse(provider.actions)
            self.assertTrue(all(t.ok for t in observation.turn_results))
            for history in provider.histories:
                for message in history:
                    if message.role=='tool' and message.content:
                        data=json.loads(message.content)
                        raw=json.dumps(data)
                        if 'Ran ' in raw and message.tool_call_id not in seen_calls:
                            receipts.append(raw)
                            seen_calls.add(message.tool_call_id)
            self.assertTrue(receipts,'real public subprocess output missing')
            self.assertEqual(sum(files is not None for files in phases),len(receipts))
            for receipt in receipts:
                found=re.search(r'Ran (\d+) tests?',receipt)
                self.assertIsNotNone(found)
                self.assertGreater(int(found[1]),0)
            return result
        result,processes,_=isolated(case,'v31-runtime-root',{},executor=execute)
        self.assertEqual('passed',result.status,(result,processes))
        self.assertTrue(all(p['tests'] and p['exit_code']==0 and not p['timed_out'] and not p['cleanup_failed'] for p in processes))
        root=os.environ.get('TRICODER_V3_EVIDENCE')
        if root:
            with (Path(root)/(case_id+'-runtime-turns.json')).open('x',encoding='utf8') as f:
                json.dump({'turns':[asdict(t) for t in observation.turn_results],'public':receipts},f,ensure_ascii=False,indent=2)
    return test


class RootRuntimeTests(unittest.TestCase):
    pass

for case_id in REFERENCES:
    setattr(RootRuntimeTests,'test_'+case_id.replace('-','_'),runtime_test(case_id))


def load_tests(loader,tests,pattern):
    # Reuse all original 125+26 tests with new cases and separate exclusive receipts.
    for module in (variants,fixes):
        for name,cls in vars(module).items():
            if not isinstance(cls,type) or not issubclass(cls,unittest.TestCase) or cls.__module__!=module.__name__: continue
            if not loader.getTestCaseNames(cls): continue
            def setup_class(cls): cls.cases=cases(SUITE)
            def setup(self):
                self.old_evidence=os.environ.get('TRICODER_V3_EVIDENCE')
                if self.old_evidence: os.environ['TRICODER_V3_EVIDENCE']=str(Path(self.old_evidence)/'v3-1-variants')
                # Keep receipts fully named, but avoid Windows CreateProcess cwd limits.
                def short_workspace(*args, **kwargs):
                    return isolated(*args, **kwargs, workspace_prefix='v31-')
                for target in (variants, fixes):
                    self.enterContext(patch.object(target, 'isolated', side_effect=short_workspace))
            def teardown(self):
                if self.old_evidence: os.environ['TRICODER_V3_EVIDENCE']=self.old_evidence
            migrated=type('V31'+name,(cls,),{'setUpClass':classmethod(setup_class),'setUp':setup,'tearDown':teardown})
            tests.addTests(loader.loadTestsFromTestCase(migrated))
    return tests
