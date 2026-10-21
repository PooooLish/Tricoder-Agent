"""版本化题库的公开需求、真实 matcher 和原隐藏测试正反验证。"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tricoder.evals.loader import load_suite
from tricoder.evals.runner import _path_is_allowed, _run_verification
from tricoder.evals.workspace import install_verifier
from tests import test_eval_quality_suite as v1_tests

ROOT = Path(__file__).parents[1]
V1 = ROOT / "evals/quality-v1"
V2 = ROOT / "evals/quality-v2"


class EvalQualityV2SuiteTests(unittest.TestCase):
    def suite(self):
        self.assertTrue(V2.is_dir(), "缺少版本化 quality-v2 公开契约题库")
        return load_suite(V2)

    def test_version_ids_budgets_splits_and_verifiers_are_preserved(self):
        new, old = self.suite(), load_suite(V1)
        self.assertEqual((2, "quality-v2", "quality-v2.1"), (new.schema_version, new.id, new.benchmark_version))
        self.assertEqual({"quality": 19, "contract": 11}, dict(Counter(c.execution_kind for c in new.cases)))
        self.assertEqual([c.id for c in old.cases], [c.id for c in new.cases])
        for before, after in zip(old.cases, new.cases):
            self.assertEqual((before.split, 8, before.required_changes), (after.split, after.max_rounds, after.required_changes))
            for path in before.verifier_dir.rglob("*.py"):
                self.assertEqual(path.read_bytes(), (after.verifier_dir / path.relative_to(before.verifier_dir)).read_bytes())
            if before.execution_kind == "contract":
                self.assertEqual((before.source_dir / "case.toml").read_bytes(), (after.source_dir / "case.toml").read_bytes())
        baseline = json.loads((V2 / "source-v1-sha256.json").read_text(encoding="utf8"))
        for relative, expected in baseline.items():
            self.assertEqual(expected, hashlib.sha256((V1 / relative).read_bytes()).hexdigest(), relative)

    def test_quality_test_paths_follow_actual_matcher_and_production_scope(self):
        for case in self.suite().cases:
            if case.execution_kind != "quality":
                self.assertFalse(case.expose_edit_scope)
                self.assertEqual("legacy", case.path_match_mode)
                continue
            self.assertTrue(case.expose_edit_scope)
            self.assertEqual("workspace_root", case.path_match_mode)
            self.assertEqual(("tests", "tests/*.py"), case.allowed_changes[-2:])
            for path in ("tests", "tests/test_x.py"):
                self.assertTrue(_path_is_allowed(path, case.allowed_changes, mode=case.path_match_mode), (case.id, path))
            for path in ("unrelated.py", "test_x.py", "tests/tests", "tests/tests/test_x.py", "other/tests/test_x.py", "tests/nested", "tests/nested/test_x.py", ".tricoder_eval_verifier/test_x.py"):
                self.assertFalse(_path_is_allowed(path, case.allowed_changes, mode=case.path_match_mode), (case.id, path))

    def test_four_explicit_semantics_and_late_correction(self):
        cases = {c.id: c for c in self.suite().cases}
        for case_id, words in {
            "rc-invalid-args": ("首尾", "小写", "内部空白", "空字符串", "模拟"),
            "rc-patch-conflict": ("所有", "无匹配", "模拟", "重读"),
            "dl-regression": ("None", "a/b"),
        }.items():
            case = cases[case_id]
            for word in words:
                self.assertIn(word, case.task)
                self.assertIn(word, case.steps[0].content)
        latest = cases["mm-latest-correction"]
        self.assertEqual("初始决定使用 red。", latest.steps[0].content)
        self.assertIn("空字符串", latest.steps[1].content)
        self.assertIn("initial", latest.steps[1].content)

    def test_each_unchanged_hidden_verifier_accepts_correct_and_rejects_wrong(self):
        with tempfile.TemporaryDirectory() as temporary:
            for case in self.suite().cases:
                for correct in (True, False):
                    with self.subTest(case=case.id, correct=correct):
                        workspace = Path(temporary) / f"{case.id}-{correct}"
                        shutil.copytree(case.workspace_dir, workspace)
                        changes = v1_tests._SOLUTIONS.get(case.id, ()) if correct else ((v1_tests._NEGATIVE_MUTATIONS[case.id],) if case.id in v1_tests._NEGATIVE_MUTATIONS else ())
                        v1_tests.EvalQualitySuiteTests._apply(workspace, changes)
                        install_verifier(case, workspace)
                        results = [_run_verification(spec, workspace) for spec in case.verifications]
                        self.assertEqual(correct, all(r.passed for r in results), results)
