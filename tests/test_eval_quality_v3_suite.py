"""The V3 scheduler must run 30 quality tasks without repurposing old contracts."""
from collections import Counter
import ast
import hashlib
import json
import unittest

from quality_v3_support import NEW_CASES, OLD, SUITE
from tricoder.evals.experiment import plan_trials
from tricoder.evals.loader import load_experiment, load_suite


class QualityV3SuiteTests(unittest.TestCase):
    def suite(self):
        self.assertTrue((SUITE / "suite.toml").is_file(), "quality-v3 suite is not implemented")
        return load_suite(SUITE)

    def test_pure_quality_schedule_split_categories_and_budget(self):
        suite = self.suite()
        old = {c.id for c in load_suite(OLD).cases if c.execution_kind == "quality"}
        self.assertEqual(("quality-v3", "quality-v3", 2), (suite.id, suite.benchmark_version, suite.schema_version))
        self.assertEqual(30, len(suite.cases))
        self.assertEqual(old | set(NEW_CASES), {c.id for c in suite.cases})
        self.assertEqual({"quality": 30}, dict(Counter(c.execution_kind for c in suite.cases)))
        self.assertEqual({"dev": 22, "holdout": 8}, dict(Counter(c.split for c in suite.cases)))
        self.assertEqual(dict(single_file=8, cross_file=11, recovery=4, memory=4, delivery=3),
                         dict(Counter(c.category for c in suite.cases)))
        for c in suite.cases:
            with self.subTest(case=c.id):
                self.assertEqual((16, 16000), (c.max_rounds, c.max_context_chars))
                self.assertEqual("workspace_root", c.path_match_mode)
                self.assertTrue(c.expose_edit_scope)
                self.assertEqual(("hidden_verifier",), c.scorers)
                self.assertEqual("dev" if c.id in old | set(list(NEW_CASES)[:3]) else "holdout", c.split)
        provider = next(c for c in suite.cases if c.id == "rc-provider-transient")
        self.assertEqual(("single_file", ("none",)), (provider.category, provider.faults))
        self.assertNotIn("fault_triggered", provider.dimensions)
        self.assertNotIn("recovered", provider.dimensions)
        interface = next(c for c in suite.cases if c.id == "cf-module-interface")
        self.assertEqual(("formatter.py", "api.py", "tests/*.py"), interface.allowed_changes)
        self.assertEqual(("*.py",), interface.required_changes)

    def test_four_manifests_produce_exact_real_trial_identities(self):
        self.suite()
        for name, size, repeats, split, seconds in (
            ("baseline", 30, 1, "all", 7200), ("repeat3", 90, 3, "all", 21600),
            ("dev", 22, 1, "dev", 7200), ("holdout", 8, 1, "holdout", 7200),
        ):
            with self.subTest(manifest=name):
                experiment = load_experiment(SUITE / f"experiment-quality-{name}.toml")
                planned = plan_trials(experiment)
                self.assertEqual((size, size, repeats, split, seconds, 23),
                    (len(planned), experiment.max_trials, experiment.repetitions,
                     experiment.split, experiment.time_budget_seconds, experiment.seed))
                self.assertEqual(size, len({p.key for p in planned}))
                for trial in planned:
                    c = trial.condition
                    self.assertEqual(("deepseek", "deepseek-v4-flash", "off", "off", "quality", ("none",)),
                        (c.provider, c.model, c.memory_compaction, c.memory_persistence, c.execution_kind, c.faults))
                    self.assertEqual("quality", trial.case.execution_kind)

    def test_source_manifest_preserves_every_old_regular_file(self):
        self.suite()
        manifest = json.loads((SUITE / "source-v2.2-sha256.json").read_text("utf8"))
        actual = {p.relative_to(OLD).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in OLD.rglob("*") if p.is_file()}
        self.assertEqual(actual, manifest)
        matrix = json.loads((SUITE / "case-matrix.json").read_text("utf8"))
        self.assertEqual({c.id for c in self.suite().cases}, {row["id"] for row in matrix})
        fields = {"id", "source_case", "category", "split", "difficulty", "task_family",
                  "requirements", "verification_map", "fault_evidence", "limitations"}
        for row in matrix:
            self.assertEqual(fields, set(row))
            self.assertEqual(None if row["id"] in NEW_CASES else
                             f"evals/quality-v2.2/cases/{row['id']}", row["source_case"])

    def test_fixture_resources_scope_and_requirement_maps(self):
        matrix={row["id"]:row for row in json.loads((SUITE/"case-matrix.json").read_text("utf8"))}
        for case in self.suite().cases:
            with self.subTest(case=case.id):
                workspace=case.source_dir/"workspace"
                for relative in ("README.md","tests/__init__.py","tests/test_public.py"):
                    self.assertTrue((workspace/relative).is_file(),(case.id,relative))
                for path in workspace.rglob("*"):
                    self.assertFalse(path.is_symlink())
                    if path.is_file():
                        self.assertIn(path.suffix,(".py",".md",".txt",".log"))
                        self.assertNotIn(path.name,("quality_v3_support.py","test_hidden.py","case.toml"))
                self.assertIn("tests/*.py",case.allowed_changes)
                self.assertNotIn("tests",case.allowed_changes)
                self.assertNotIn("*.py",case.allowed_changes)  # enumerate production files
                self.assertFalse(any("**" in p for p in case.allowed_changes))
                row=matrix[case.id]
                self.assertEqual(set(row["requirements"]),set(row["verification_map"]))
                public=ast.parse((workspace/"tests/test_public.py").read_text("utf8"))
                hidden=ast.parse((case.source_dir/"verifier/test_hidden.py").read_text("utf8"))
                methods={"Behavior."+node.name for tree in (public,hidden)
                         for node in ast.walk(tree) if isinstance(node,ast.FunctionDef)}
                self.assertEqual({"Behavior.test_public","Behavior.test_normal","Behavior.test_boundary",
                                  "Behavior.test_regression"},methods)
                for names in row["verification_map"].values():
                    self.assertTrue(names and set(names)<=methods)
