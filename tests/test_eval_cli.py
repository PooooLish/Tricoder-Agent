"""CLI parsing and routing tests for ``tricoder eval``."""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from tricoder.cli import build_parser, main


class EvalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_cwd = Path.cwd()
        self.suite_dir = self._write_suite()

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)
        self.temporary.cleanup()

    def _write_suite(self) -> Path:
        suite_dir = self.root / "suite"
        case_dir = suite_dir / "cases" / "case-one"
        (case_dir / "workspace").mkdir(parents=True)
        (case_dir / "verifier").mkdir()
        (case_dir / "workspace" / "app.py").write_text(
            "value = 1\n", encoding="utf-8"
        )
        (case_dir / "verifier" / "test_hidden.py").write_text(
            "import unittest\n\nclass HiddenTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (suite_dir / "suite.toml").write_text(
            'id = "smoke"\ntitle = "Smoke"\ncases = ["case-one"]\n',
            encoding="utf-8",
        )
        (case_dir / "case.toml").write_text(
            'id = "case-one"\n'
            'title = "Case one"\n'
            'task = "TASK-SECRET-SENTINEL"\n'
            'allowed_changes = ["app.py"]\n'
            'required_changes = ["app.py"]\n'
            "max_rounds = 4\n"
            "max_context_chars = 4000\n\n"
            "[[verification]]\n"
            'name = "hidden"\n'
            'command = "python -m unittest discover -s .tricoder_eval_verifier -q"\n'
            "timeout = 5\n",
            encoding="utf-8",
        )
        return suite_dir

    def test_eval_parser_defaults_to_real_openai(self) -> None:
        """Removing the eval parser or changing its real-provider default must fail."""
        args = build_parser().parse_args(["eval", "evals/smoke"])

        self.assertEqual("eval", args.command)
        self.assertEqual(Path("evals/smoke"), args.suite)
        self.assertEqual("openai", args.provider)
        self.assertFalse(args.dry_run)

    def test_eval_parser_exposes_only_eval_specific_runtime_options(self) -> None:
        """Adding workspace/read-only/audit controls to eval would break its boundary."""
        args = build_parser().parse_args(
            [
                "eval",
                "evals/smoke",
                "--provider",
                "glm",
                "--model",
                "glm-eval",
                "--base-url",
                "https://example.test/v1",
                "--env-file",
                "D:/keys.env",
                "--case",
                "case-one",
                "--dry-run",
                "--no-color",
            ]
        )

        self.assertEqual("glm", args.provider)
        self.assertEqual("case-one", args.case)
        self.assertTrue(args.dry_run)
        for forbidden in ("--workspace", "--read-only", "--audit-dir"):
            with self.subTest(forbidden=forbidden), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as captured:
                    build_parser().parse_args(
                        ["eval", "evals/smoke", forbidden, "unexpected"]
                    )
                self.assertEqual(2, captured.exception.code)

    def test_eval_dry_run_does_not_load_config_build_provider_or_create_state(
        self,
    ) -> None:
        """Moving dry-run after runtime assembly would create state or touch credentials."""
        output = io.StringIO()
        provider_calls: list[str] = []
        os.chdir(self.root)

        with patch(
            "tricoder.evals.service.load_config",
            side_effect=AssertionError("dry-run must not load config"),
        ):
            exit_code = main(
                [
                    "eval",
                    str(self.suite_dir),
                    "--env-file",
                    str(self.root / "missing.env"),
                    "--dry-run",
                    "--no-color",
                ],
                environ={},
                provider_factory=lambda *_args: provider_calls.append(  # type: ignore[arg-type]
                    "called"
                ),
                output=output,
            )

        self.assertEqual(0, exit_code)
        self.assertEqual([], provider_calls)
        self.assertFalse((self.root / "runtime").exists())
        self.assertIn("case-one", output.getvalue())
        self.assertNotIn("TASK-SECRET-SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
