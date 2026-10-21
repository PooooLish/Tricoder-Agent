"""Versioned file-scope scoring, exercised through the real isolated scorer."""
from dataclasses import fields, replace
from pathlib import Path, PurePosixPath
import json
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from tricoder.evals.compare import ComparisonError, compare_report_payloads
from tricoder.evals.loader import EvalDefinitionError, load_suite
from tricoder.evals.models import EvalCase
from tricoder.evals.runner import _path_is_allowed, _path_matches, run_isolated_case
from tricoder.models import RunResult
from tests.test_eval_compare import _report, _trial

ROOT = Path(__file__).parents[1]


def scope_results(case, output):
    """Write actual files and retain actual snapshot/verifier/scorer results."""
    results = {}
    for directory in ("tests", "tests/tests", "other/tests", "tests/nested"):
        trial = output / directory.replace("/", "-")
        trial.mkdir()

        def executor(_case, workspace, _audit):
            (workspace / "math_ops.py").write_text("def subtract(a, b):\n    return a - b\n", encoding="utf8")
            target = workspace / directory / "test_probe.py"
            target.parent.mkdir(parents=True)
            target.write_text("# synthetic scope probe\n", encoding="utf8")
            return RunResult(True, "offline executor", 1, 1, (), "通过")

        results[directory] = run_isolated_case(case, trial, executor)
    return results


class EvalPathMatchingTests(unittest.TestCase):
    def test_real_isolated_scoring_rejects_nested_and_prefixed_files(self):
        case = next(c for c in load_suite(ROOT / "evals/quality-v2").cases if c.id == "sf-subtract")
        with tempfile.TemporaryDirectory() as temporary:
            results = scope_results(case, Path(temporary))
        for directory, result in results.items():
            with self.subTest(directory=directory):
                self.assertEqual(("math_ops.py", f"{directory}/test_probe.py"), result.modified_files)
                self.assertTrue(all(v.passed for v in result.verifications), result)
                self.assertEqual(() if directory == "tests" else ("change_out_of_scope",), result.failure_codes)
                self.assertEqual("passed" if directory == "tests" else "failed", result.status)

    def test_required_scope_uses_the_same_mode_in_real_scoring(self):
        base = load_suite(ROOT / "evals/quality-v1").cases[0]
        for mode in ("workspace_root", "legacy"):
            # A synthetic case isolates required matching without changing any real verifier.
            values = {field.name: getattr(base, field.name) for field in fields(EvalCase)}
            values.update(path_match_mode=mode, allowed_changes=("other/math_ops.py",),
                          required_changes=("math_ops.py",), verifications=())
            case = SimpleNamespace(**values)
            with tempfile.TemporaryDirectory() as temporary:
                def executor(_case, workspace, _audit):
                    (workspace / "other").mkdir()
                    (workspace / "other/math_ops.py").write_text("value = 1\n", encoding="utf8")
                    return RunResult(True, "synthetic", 1, 1, (), "通过")
                result = run_isolated_case(case, Path(temporary), executor)
            with self.subTest(mode=mode):
                self.assertEqual(("other/math_ops.py",), result.modified_files)
                self.assertEqual(("required_change_missing",) if mode == "workspace_root" else (), result.failure_codes)

    def test_segment_matching_and_default_legacy_semantics(self):
        examples = (
            ("tests/test_x.py", "tests/*.py", True),
            ("tests/tests/test_x.py", "tests/*.py", False),
            ("other/tests/test_x.py", "tests/*.py", False),
            ("tests/nested/test_x.py", "tests/*.py", False),
            ("tests/tests", "tests", False),
            ("math_ops.py", "math_ops.py", True),
            ("other/math_ops.py", "math_ops.py", False),
            ("src/a.py", "src/?.py", True),
            ("src/a.py", "src/[ab].py", True),
            ("src/c.py", "src/[ab].py", False),
            ("Tests/test_x.py", "tests/*.py", False),
            ("tests/test_x.PY", "tests/*.py", False),
            ("src/a/b.py", "src/*.py", False),
        )
        for path, pattern, expected in examples:
            with self.subTest(path=path, pattern=pattern):
                self.assertEqual(expected, _path_matches(path, pattern, mode="workspace_root"))
                self.assertEqual(PurePosixPath(path).match(pattern), _path_matches(path, pattern))
                self.assertEqual(_path_matches(path, pattern), _path_matches(path, pattern, mode="legacy"))
        for path in ("a.py", "src/a.py", "src/nested/a.py"):
            self.assertEqual(PurePosixPath(path).match("**/*.py"), _path_matches(path, "**/*.py"))

    def test_invalid_paths_modes_and_reserved_paths(self):
        for path in ("", "/a.py", "C:/a.py", "a:b.py", "tests\\a.py", "tests//a.py", "./a.py", "tests/../a.py", "tests/./a.py", "tests/"):
            with self.subTest(path=path):
                self.assertFalse(_path_matches(path, "*", mode="workspace_root"))
                self.assertFalse(_path_matches(path, path, mode="workspace_root"))
        for mode in ("unknown", "Workspace_root", None, True, 1, []):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    _path_matches("a.py", "*.py", mode=mode)
                with self.assertRaises(ValueError):
                    _path_is_allowed("a.py", (), mode=mode)
        for path in (".tricoder_eval_verifier/x.py", "a/.tricoder_eval_verifier/x.py", "runtime/tricoder-control/x.py"):
            for mode in ("legacy", "workspace_root"):
                self.assertFalse(_path_is_allowed(path, (path, "*/*", "*/*/*"), mode=mode))

    def test_loader_modes_and_new_mode_pattern_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "suite"
            shutil.copytree(ROOT / "evals/quality-v1", suite, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = suite / "cases/sf-subtract/case.toml"
            original = target.read_text(encoding="utf8")
            for raw in ('"legacy"', '"workspace_root"', '"other"', '"Workspace_root"', 'true', '1', '[]'):
                target.write_text(f"path_match_mode = {raw}\n" + original, encoding="utf8")
                with self.subTest(mode=raw):
                    if raw in ('"legacy"', '"workspace_root"'):
                        self.assertEqual(raw.strip('"'), load_suite(suite).cases[0].path_match_mode)
                    else:
                        with self.assertRaises(EvalDefinitionError):
                            load_suite(suite)
            for field in ("allowed_changes", "required_changes"):
                for pattern in ("**/*.py", "tests/a**.py", "/a.py", "C:/a.py", "a:b.py", "a\\b.py", "a//b.py", "./a.py", "a/../b.py", "a/./b.py", "a/", ""):
                    lines = [f"{field} = {json.dumps([pattern])}" if line.startswith(field + " =") else line for line in original.splitlines()]
                    target.write_text('path_match_mode = "workspace_root"\n' + "\n".join(lines), encoding="utf8")
                    with self.subTest(field=field, pattern=pattern):
                        with self.assertRaises(EvalDefinitionError):
                            load_suite(suite)
            # Legacy globstar remains accepted by the loader and matcher.
            target.write_text(original.replace('allowed_changes = ["math_ops.py"]', 'allowed_changes = ["**/*.py"]'), encoding="utf8")
            case = load_suite(suite).cases[0]
            self.assertEqual("legacy", case.path_match_mode)
            self.assertEqual(("**/*.py",), case.allowed_changes)
            legacy = Path(temporary) / "legacy"
            shutil.copytree(ROOT / "evals/smoke", legacy)
            target = next(legacy.glob("cases/*/case.toml"))
            target.write_text('path_match_mode = "legacy"\n' + target.read_text(encoding="utf8"), encoding="utf8")
            with self.assertRaises(EvalDefinitionError):
                load_suite(legacy)

    def test_default_mode_and_versioned_comparison(self):
        for suite in ("quality-v1", "regression-v1", "smoke"):
            self.assertTrue(all(c.path_match_mode == "legacy" for c in load_suite(ROOT / "evals" / suite).cases))
        before = _report("base", "offline", [_trial("base", "one", 1, True)])
        after = _report("candidate", "offline", [_trial("candidate", "one", 1, True)])
        before.update(suite_id="quality-v2", benchmark_version="quality-v2")
        after.update(suite_id="quality-v2", benchmark_version="quality-v2.1")
        with self.assertRaisesRegex(ComparisonError, "benchmark_version"):
            compare_report_payloads(before, after)
        before["benchmark_version"] = "quality-v2.1"
        self.assertEqual(1, compare_report_payloads(before, after)["paired"])

    def test_dotted_benchmark_version_does_not_relax_suite_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "suite"
            shutil.copytree(ROOT / "evals/quality-v1", suite, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = suite / "suite.toml"
            original = target.read_text(encoding="utf8")
            for value in ("quality-v2.1", "quality-v2", "", ".v2", "v2.", "v2..1", "v2/1", "v2 1", "v2|1", "x" * 65, True, 2):
                text = original.replace('benchmark_version = "quality-v1"', 'benchmark_version = ' + json.dumps(value))
                target.write_text(text, encoding="utf8")
                with self.subTest(value=value):
                    if value in ("quality-v2.1", "quality-v2"):
                        self.assertEqual(value, load_suite(suite).benchmark_version)
                    else:
                        with self.assertRaises(EvalDefinitionError):
                            load_suite(suite)
            target.write_text(original.replace('id = "quality-v1"', 'id = "quality-v2.1"'), encoding="utf8")
            with self.assertRaises(EvalDefinitionError):
                load_suite(suite)
