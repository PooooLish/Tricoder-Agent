"""Tests for the production eval service assembly."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tricoder.evals.service import run_eval_command
from tricoder.models import ProviderConfig, ProviderResponse, ToolCall, ToolDefinition


class PassingProvider:
    """Exercise the real CodingAgent and ToolRegistry without network access."""

    def complete(
        self,
        _messages: list[object],
        _tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    "edit",
                    "edit_file",
                    {
                        "path": "app.py",
                        "old_text": "value = 1\n",
                        "new_text": "value = 2\n",
                    },
                ),
                ToolCall(
                    "verify",
                    "run_command",
                    {"command": "python -m unittest -q"},
                ),
                ToolCall("finish", "finish", {"summary": "PROVIDER-SUMMARY-SENTINEL"}),
            ),
            finish_reason="tool_calls",
        )


class FinishingWithoutChangesProvider:
    def complete(
        self,
        _messages: list[object],
        _tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        return ProviderResponse(
            tool_calls=(
                ToolCall("finish", "finish", {"summary": "finished"}),
            ),
            finish_reason="tool_calls",
        )


class ExplodingProvider:
    def complete(
        self,
        _messages: list[object],
        _tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        raise RuntimeError("PROVIDER-SECRET-SENTINEL")


class EvalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_cwd = Path.cwd()
        os.chdir(self.root)
        self.suite_dir = self._write_suite(("case-one", "case-two"))

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)
        self.temporary.cleanup()

    def _write_suite(self, case_ids: tuple[str, ...]) -> Path:
        suite_dir = self.root / "suite"
        suite_dir.mkdir()
        cases = ", ".join(f'"{case_id}"' for case_id in case_ids)
        (suite_dir / "suite.toml").write_text(
            f'id = "smoke"\ntitle = "Smoke"\ncases = [{cases}]\n',
            encoding="utf-8",
        )
        for case_id in case_ids:
            case_dir = suite_dir / "cases" / case_id
            (case_dir / "workspace").mkdir(parents=True)
            (case_dir / "verifier").mkdir()
            (case_dir / "workspace" / "app.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            (case_dir / "verifier" / "test_hidden.py").write_text(
                "import unittest\nfrom pathlib import Path\n\n"
                "class HiddenTest(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertIn('value = 2', Path('app.py').read_text(encoding='utf-8'))\n",
                encoding="utf-8",
            )
            (case_dir / "case.toml").write_text(
                f'id = "{case_id}"\n'
                f'title = "{case_id}"\n'
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

    def _args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "suite": self.suite_dir,
            "provider": "openai",
            "model": None,
            "base_url": None,
            "env_file": None,
            "case": None,
            "dry_run": False,
            "no_color": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def _environment() -> dict[str, str]:
        return {"OPENAI_API_KEY": "test-key", "TRICODER_PLAN": "0"}

    def test_real_mode_missing_key_returns_two_before_provider_or_runtime(self) -> None:
        """Loading config without a key must fail before provider/runtime creation."""
        output = io.StringIO()
        provider_calls: list[str] = []

        exit_code = run_eval_command(
            self._args(),
            environ={},
            provider_factory=lambda *_args: provider_calls.append(  # type: ignore[arg-type]
                "called"
            ),
            output=output,
        )

        self.assertEqual(2, exit_code)
        self.assertEqual([], provider_calls)
        self.assertFalse((self.root / "runtime").exists())
        self.assertNotIn("test-key", output.getvalue())
        self.assertNotIn("TASK-SECRET-SENTINEL", output.getvalue())

    def test_sensitive_fixture_is_rejected_before_provider_or_runtime(self) -> None:
        """Fixture preflight must fail before costly or writable eval setup."""
        sensitive = (
            self.suite_dir
            / "cases"
            / "case-two"
            / "workspace"
            / ".env.local"
        )
        sensitive.write_text("OPENAI_API_KEY=synthetic", encoding="utf-8")
        output = io.StringIO()
        provider_calls: list[str] = []

        exit_code = run_eval_command(
            self._args(case="case-one"),
            environ=self._environment(),
            provider_factory=lambda *_args: provider_calls.append(  # type: ignore[arg-type]
                "called"
            ),
            output=output,
        )

        self.assertEqual(2, exit_code)
        self.assertEqual([], provider_calls)
        self.assertFalse((self.root / "runtime").exists())
        self.assertNotIn("synthetic", output.getvalue())

    def test_real_mode_success_filters_case_and_writes_runtime_report(self) -> None:
        """Bypassing production assembly, case filtering, or cwd output must fail."""
        output = io.StringIO()
        provider_configs: list[tuple[ProviderConfig, float]] = []

        def factory(config: ProviderConfig, timeout: float) -> PassingProvider:
            provider_configs.append((config, timeout))
            return PassingProvider()

        exit_code = run_eval_command(
            self._args(case="case-two", model="eval-model"),
            environ=self._environment(),
            provider_factory=factory,
            output=output,
        )

        run_dirs = list((self.root / "runtime" / "evals").iterdir())
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(provider_configs))
        self.assertEqual("openai", provider_configs[0][0].name)
        self.assertEqual("eval-model", provider_configs[0][0].model)
        self.assertEqual(1, len(run_dirs))
        self.assertFalse(
            (run_dirs[0] / "workspaces" / "case-one").exists()
        )
        self.assertTrue((run_dirs[0] / "workspaces" / "case-two").is_dir())
        payload = json.loads((run_dirs[0] / "result.json").read_text("utf-8"))
        self.assertEqual("passed", payload["cases"][0]["status"])
        self.assertEqual("case-two", payload["cases"][0]["case_id"])
        self.assertIn(str(run_dirs[0] / "report.md"), output.getvalue())
        self.assertNotIn("test-key", output.getvalue())
        self.assertNotIn("TASK-SECRET-SENTINEL", output.getvalue())
        self.assertNotIn("PROVIDER-SUMMARY-SENTINEL", output.getvalue())

    def test_case_failure_returns_one_and_still_writes_report(self) -> None:
        """Treating a scored case failure as success or omitting its report must fail."""
        output = io.StringIO()

        exit_code = run_eval_command(
            self._args(case="case-one"),
            environ=self._environment(),
            provider_factory=lambda _config, _timeout: FinishingWithoutChangesProvider(),
            output=output,
        )

        run_dir = next((self.root / "runtime" / "evals").iterdir())
        payload = json.loads((run_dir / "result.json").read_text("utf-8"))
        self.assertEqual(1, exit_code)
        self.assertEqual("failed", payload["cases"][0]["status"])
        self.assertTrue((run_dir / "report.md").is_file())

    def test_provider_exception_is_reported_without_exception_text(self) -> None:
        """Leaking provider exception text through terminal/report must fail."""
        output = io.StringIO()

        exit_code = run_eval_command(
            self._args(case="case-one"),
            environ=self._environment(),
            provider_factory=lambda _config, _timeout: ExplodingProvider(),
            output=output,
        )

        run_dir = next((self.root / "runtime" / "evals").iterdir())
        result_text = (run_dir / "result.json").read_text("utf-8")
        report_text = (run_dir / "report.md").read_text("utf-8")
        self.assertEqual(1, exit_code)
        self.assertNotIn("PROVIDER-SECRET-SENTINEL", output.getvalue())
        self.assertNotIn("PROVIDER-SECRET-SENTINEL", result_text)
        self.assertNotIn("PROVIDER-SECRET-SENTINEL", report_text)

    def test_output_parent_is_rejected_before_provider_or_eval_state(self) -> None:
        """A linked runtime parent must be rejected before runner state is created."""
        runtime = self.root / "runtime"
        runtime.mkdir()
        provider_calls: list[str] = []
        output = io.StringIO()

        with patch(
            "tricoder.evals.output._is_link_or_reparse_point",
            side_effect=lambda path: path == runtime,
        ):
            exit_code = run_eval_command(
                self._args(case="case-one"),
                environ=self._environment(),
                provider_factory=lambda *_args: provider_calls.append(  # type: ignore[arg-type]
                    "called"
                ),
                output=output,
            )

        self.assertEqual(2, exit_code)
        self.assertEqual([], provider_calls)
        self.assertFalse((runtime / "evals").exists())

    def test_fixed_timestamp_collision_creates_a_new_exclusive_run(self) -> None:
        """Two runs with the same clock value must not share state or reports."""
        fixed_run_id = "20260825t010203.000000z"
        existing = self.root / "runtime" / "evals" / fixed_run_id
        existing.mkdir(parents=True)
        marker = existing / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        output = io.StringIO()

        with patch("tricoder.evals.service.datetime") as clock:
            clock.now.return_value.strftime.return_value = fixed_run_id
            exit_code = run_eval_command(
                self._args(case="case-one"),
                environ=self._environment(),
                provider_factory=lambda _config, _timeout: FinishingWithoutChangesProvider(),
                output=output,
            )

        run_root = self.root / "runtime" / "evals"
        self.assertEqual(1, exit_code)
        self.assertEqual(
            (fixed_run_id, f"{fixed_run_id}-01"),
            tuple(sorted(path.name for path in run_root.iterdir())),
        )
        self.assertEqual((marker,), tuple(existing.iterdir()))
        self.assertIn(f"run_id={fixed_run_id}-01", output.getvalue())

    def test_definition_cwd_and_config_exceptions_are_fixed_and_redacted(self) -> None:
        """Unexpected boundary failures must return 2 without traceback text."""
        for target in (
            "tricoder.evals.service.load_suite",
            "tricoder.evals.service.Path.cwd",
            "tricoder.evals.service.load_config",
        ):
            with self.subTest(target=target):
                output = io.StringIO()
                with patch(
                    target,
                    side_effect=RuntimeError("BOUNDARY-SECRET-SENTINEL"),
                ):
                    exit_code = run_eval_command(
                        self._args(case="case-one"),
                        environ=self._environment(),
                        provider_factory=lambda _config, _timeout: (
                            FinishingWithoutChangesProvider()
                        ),
                        output=output,
                    )

                self.assertEqual(2, exit_code)
                self.assertNotIn("BOUNDARY-SECRET-SENTINEL", output.getvalue())

    def test_base_url_and_env_file_reach_production_provider_config(self) -> None:
        """Dropping explicit connection options before provider construction must fail."""
        env_file = self.root / "eval.env"
        env_file.write_text("OPENAI_API_KEY=file-test-key\n", encoding="utf-8")
        provider_configs: list[ProviderConfig] = []

        def factory(
            config: ProviderConfig,
            _timeout: float,
        ) -> FinishingWithoutChangesProvider:
            provider_configs.append(config)
            return FinishingWithoutChangesProvider()

        exit_code = run_eval_command(
            self._args(
                case="case-one",
                base_url="https://example.test/v1",
                env_file=env_file,
            ),
            environ={"TRICODER_PLAN": "0"},
            provider_factory=factory,
            output=io.StringIO(),
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(1, len(provider_configs))
        self.assertEqual("https://example.test/v1", provider_configs[0].base_url)
        self.assertEqual("file-test-key", provider_configs[0].api_key)

    def test_unknown_case_returns_two_without_provider_or_runtime(self) -> None:
        """An unknown case must stop before config, Provider, or output creation."""
        provider_calls: list[str] = []

        exit_code = run_eval_command(
            self._args(case="missing-case"),
            environ=self._environment(),
            provider_factory=lambda *_args: provider_calls.append(  # type: ignore[arg-type]
                "called"
            ),
            output=io.StringIO(),
        )

        self.assertEqual(2, exit_code)
        self.assertEqual([], provider_calls)
        self.assertFalse((self.root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
