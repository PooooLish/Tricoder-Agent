"""Exact public contract delta; fixtures and historical suite remain immutable."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import tomllib
import unittest

from delivery_helpers import DeliveryHarness, finish
from tricoder.evals.loader import load_suite

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / 'evals/quality-v2'
NEW = ROOT / 'evals/quality-v2.2'
NONINTERACTIVE = '当前为非交互执行；已有信息足够时自主完成。确实缺少会改变结果的必要信息时，应明确报告无法继续，不能编造回答。调用 ask_user 将无法获得即时回答。'
ACK = '本轮只确认并记住这条约束/决定，不修改文件、不运行测试；确认后结束本轮，等待下一条任务'
MEMORY = {'mm-early-constraint', 'mm-latest-correction', 'mm-todo-once'}


class QualityV22Tests(unittest.TestCase):
    def test_exact_public_delta_and_unchanged_fixture_hashes(self):
        old, new = load_suite(OLD), load_suite(NEW)
        self.assertEqual(('quality-v2-2','quality-v2.2'),(new.id,new.benchmark_version))
        self.assertEqual({'quality':19,'contract':11},dict(Counter(c.execution_kind for c in new.cases)))
        self.assertEqual([c.id for c in old.cases],[c.id for c in new.cases])
        hashes=json.loads((NEW/'source-v2.1-sha256.json').read_text(encoding='utf8'))
        for rel, expected in hashes.items():
            self.assertEqual(expected,hashlib.sha256((OLD/rel).read_bytes()).hexdigest(),rel)
            if '/workspace/' in rel or '/verifier/' in rel:
                self.assertEqual((OLD/rel).read_bytes(),(NEW/rel).read_bytes(),rel)
        for before, after in zip(old.cases,new.cases):
            b=tomllib.loads((before.source_dir/'case.toml').read_text(encoding='utf8'))
            a=tomllib.loads((after.source_dir/'case.toml').read_text(encoding='utf8'))
            if before.execution_kind=='contract':
                self.assertEqual((before.source_dir/'case.toml').read_bytes(),(after.source_dir/'case.toml').read_bytes())
            else:
                self.assertEqual('workspace_root',after.path_match_mode)
                for i, step in enumerate(b['steps']):
                    if step['kind']=='user_turn':
                        step['content'] += '\n'+NONINTERACTIVE
                        if before.id in MEMORY and i==0:step['content']+='\n'+ACK
                self.assertEqual(b,a,before.id)
            self.assertEqual(8,after.max_rounds)
        cases={c.id:c for c in new.cases}
        self.assertNotIn('latest',cases['mm-latest-correction'].steps[0].content)
        self.assertNotIn('空字符串',cases['mm-latest-correction'].steps[0].content)

    def test_manifest_and_confirmation_needs_no_tools_or_checks(self):
        data=tomllib.loads((NEW/'experiment-quality-baseline.toml').read_text(encoding='utf8'))
        old=tomllib.loads((OLD/'experiment-quality-baseline.toml').read_text(encoding='utf8'))
        old['experiment_id']='quality-v2-2-baseline'
        self.assertEqual(old,data)
        for case in load_suite(NEW).cases:
            if case.id in MEMORY:
                with DeliveryHarness([finish()]) as h:
                    result=h.run(case.steps[0].content)
                    self.assertTrue(result.ok)
                    self.assertEqual((),result.modified_files)
                    self.assertEqual((),result.task_validation.records)
                    self.assertFalse(result.current_verification_required)
