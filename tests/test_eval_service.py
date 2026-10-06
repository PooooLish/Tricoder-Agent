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

from tricoder.evals.models import EvalCase, ScenarioStep
from tricoder.evals.scenarios import RuntimeScenarioObservation
from tricoder.evals.service import _execute_case, run_eval_command
from tricoder.core.events import (
    ProviderCompleted,
    TextDelta,
    ToolCallCompleted,
    UsageReported,
)
from tricoder.models import (
    AppConfig,
    ProviderConfig,
    ProviderResponse,
    RunResult,
    ToolCall,
    ToolDefinition,
)
from tricoder.models import TokenUsage
from tricoder.session.runtime import SessionRuntimeError
from tricoder.workspace.lock import WorkspaceLock


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


class MultiTurnMemoryProvider:
    """同时模拟业务工具调用和生产记忆摘要流。"""

    def __init__(self) -> None:
        self.business_calls = 0
        self.summary_calls = 0

    def complete(
        self,
        _messages: list[object],
        _tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.business_calls += 1
        calls: tuple[ToolCall, ...]
        if self.business_calls == 1:
            calls = (
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
                ToolCall("finish-1", "finish", {"summary": "first turn"}),
            )
        else:
            calls = (ToolCall("finish-2", "finish", {"summary": "second turn"}),)
        return ProviderResponse(
            tool_calls=calls,
            finish_reason="tool_calls",
            usage=TokenUsage(input_tokens=7, output_tokens=2),
        )

    async def stream(self, _messages, _tools=(), *, cancellation=None):  # type: ignore[no-untyped-def]
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if _tools:
            response = self.complete(_messages, _tools)
            for call in response.tool_calls:
                yield ToolCallCompleted(call)
            assert response.usage is not None
            yield UsageReported(response.usage)
            yield ProviderCompleted(response.finish_reason)
            return
        self.summary_calls += 1
        yield TextDelta(
            '{"goal":null,"constraints":[],"decisions":[],"open_items":[]}'
        )
        yield UsageReported(TokenUsage(input_tokens=3, output_tokens=1))
        yield ProviderCompleted("stop")


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
            (case_dir / "workspace" / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_workspace_is_runnable(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
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
            "experiment": None,
            "repeat": 1,
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

    def test_repeat_rebuilds_provider_and_writes_each_trial_result(self) -> None:
        """Repeats must be independent runs rather than retries inside one Agent."""
        output = io.StringIO()
        provider_calls: list[ProviderConfig] = []

        def factory(config: ProviderConfig, _timeout: float) -> PassingProvider:
            provider_calls.append(config)
            return PassingProvider()

        exit_code = run_eval_command(
            self._args(case="case-two", model="eval-model", repeat=2),
            environ=self._environment(),
            provider_factory=factory,
            output=output,
        )

        run_dir = next((self.root / "runtime" / "evals").iterdir())
        payload = json.loads((run_dir / "result.json").read_text("utf-8"))
        self.assertEqual(0, exit_code)
        self.assertEqual(2, len(provider_calls))
        self.assertEqual(2, len(payload["trials"]))
        self.assertEqual(2, len(list((run_dir / "results").glob("*.json"))))
        self.assertEqual(2, payload["schema_version"])
        self.assertEqual("legacy", payload["benchmark_version"])
        self.assertEqual("default", payload["conditions"][0]["id"])
        self.assertEqual(
            {"suite", "verifier", "tasks", "budgets", "approval_policy", "environment", "code"},
            set(payload["fingerprints"]),
        )

    def test_experiment_uses_real_multi_turn_and_memory_configuration(self) -> None:
        """只记录 memory-on 标签、却未触发生产摘要路径时必须失败。"""
        (self.suite_dir / "suite.toml").write_text(
            'schema_version = 2\nbenchmark_version = "quality-v1"\n'
            'id = "smoke"\ntitle = "Smoke"\ncases = ["case-one"]\n',
            encoding="utf-8",
        )
        case_path = self.suite_dir / "cases" / "case-one" / "case.toml"
        case_path.write_text(
            'id = "case-one"\ntitle = "case-one"\ntask = "fallback"\n'
            'allowed_changes = ["app.py"]\nrequired_changes = ["app.py"]\n'
            # 命令白名单说明属于生产请求固定前缀；为本用例保留两轮各一次
            # 摘要的原验证目标，预算需覆盖扩充后的固定前缀，而不是误测第三次压缩。
            'max_rounds = 4\nmax_context_chars = 5200\n'
            'category = "memory"\nsplit = "dev"\nexecution_kind = "quality"\n'
            'scorers = ["hidden_verifier"]\nfaults = ["none"]\n'
            'dimensions = ["artifact_correct", "agent_completed"]\n\n'
            '[[steps]]\nkind = "user_turn"\ncontent = "先修复文件"\n\n'
            '[[steps]]\nkind = "user_turn"\ncontent = "确认约束仍然有效"\n\n'
            '[[verification]]\nname = "hidden"\n'
            'command = "python -m unittest discover -s .tricoder_eval_verifier -q"\n'
            'timeout = 5\n',
            encoding="utf-8",
        )
        experiment = self.root / "experiment.toml"
        experiment.write_text(
            'schema_version = 1\nexperiment_id = "memory-path"\n'
            'suite = "suite"\nrepetitions = 1\nsplit = "all"\n'
            'max_trials = 1\ntime_budget_seconds = 60\nseed = 1\n\n'
            '[[conditions]]\nid = "memory-on"\nprovider = "openai"\n'
            'model = "offline"\nexecution_kind = "quality"\n'
            'memory_compaction = "structured"\n'
            'memory_persistence = "reviewed_summary"\n'
            'scorers = ["hidden_verifier"]\nfaults = ["none"]\n',
            encoding="utf-8",
        )
        providers: list[MultiTurnMemoryProvider] = []

        def factory(_config: ProviderConfig, _timeout: float) -> MultiTurnMemoryProvider:
            provider = MultiTurnMemoryProvider()
            providers.append(provider)
            return provider

        exit_code = run_eval_command(
            self._args(suite=None, experiment=experiment),
            environ=self._environment(),
            provider_factory=factory,
            output=io.StringIO(),
        )

        run_dir = next((self.root / "runtime" / "evals").iterdir())
        payload = json.loads((run_dir / "result.json").read_text("utf-8"))
        trial = payload["trials"][0]
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(providers))
        self.assertEqual(2, providers[0].business_calls)
        self.assertEqual(2, providers[0].summary_calls)
        self.assertTrue(trial["summary_triggered"])
        self.assertEqual(2, trial["summary_count"])
        self.assertEqual(6, trial["summary_usage"]["input_tokens"])
        self.assertEqual(2, trial["summary_usage"]["output_tokens"])

    def test_experiment_records_triggered_fault_and_recovery_path(self) -> None:
        """声明故障但未包装生产 Provider 时，不得伪造恢复成功。"""
        experiment = self.root / "fault-experiment.toml"
        experiment.write_text(
            'schema_version = 1\nexperiment_id = "fault-path"\n'
            'suite = "suite"\nrepetitions = 1\nsplit = "all"\n'
            'max_trials = 2\ntime_budget_seconds = 60\nseed = 1\n\n'
            '[[conditions]]\nid = "provider-retry"\nprovider = "openai"\n'
            'model = "offline"\nexecution_kind = "quality"\n'
            'memory_compaction = "off"\nmemory_persistence = "off"\n'
            'scorers = ["hidden_verifier"]\nfaults = ["provider_transient"]\n',
            encoding="utf-8",
        )

        exit_code = run_eval_command(
            self._args(suite=None, experiment=experiment),
            environ=self._environment(),
            provider_factory=lambda _config, _timeout: PassingProvider(),
            output=io.StringIO(),
        )

        run_dir = next((self.root / "runtime" / "evals").iterdir())
        payload = json.loads((run_dir / "result.json").read_text("utf-8"))
        self.assertEqual(0, exit_code)
        self.assertEqual(2, len(payload["trials"]))
        for trial in payload["trials"]:
            self.assertEqual(["provider_transient"], trial["triggered_faults"])
            self.assertEqual("provider_transport_retry", trial["recovery_path"])
            self.assertEqual(1, trial["retries"])
            self.assertTrue(trial["dimensions"]["fault_triggered"])
            self.assertTrue(trial["dimensions"]["recovered"])

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

    def test_control_steps_use_temporary_session_runtime_and_restart(self) -> None:
        """控制步骤不得落入普通多轮分支或作为文本发送给模型。"""
        workspace = self.root / "control-workspace"
        workspace.mkdir()
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        (workspace / "test_smoke.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        case = EvalCase(
            id="control",
            title="control",
            task="fallback",
            source_dir=self.root,
            workspace_dir=workspace,
            verifier_dir=self.root,
            allowed_changes=("app.py",),
            required_changes=("app.py",),
            max_rounds=4,
            max_context_chars=4000,
            verifications=(),
            category="memory",
            steps=(
                ScenarioStep("user_turn", content="记住约束"),
                ScenarioStep("memory_save"),
                ScenarioStep("restart_session"),
                ScenarioStep("user_turn", content="现在修改文件"),
            ),
        )
        config = AppConfig(
            workspace=workspace,
            provider=ProviderConfig(
                "openai", "synthetic", "https://example.invalid/v1", "offline"
            ),
            audit_dir=self.root / "audit",
            plan_enabled=False,
        )
        builds = 0

        def factory(_config: ProviderConfig, _timeout: float):  # type: ignore[no-untyped-def]
            nonlocal builds
            builds += 1
            return FinishingWithoutChangesProvider() if builds == 1 else PassingProvider()

        result = _execute_case(
            case,
            workspace,
            self.root / "audit" / "control.jsonl",
            config,
            factory,
        )

        self.assertTrue(result.ok, result)
        self.assertEqual(2, builds)
        self.assertEqual("value = 2\n", (workspace / "app.py").read_text("utf-8"))
        self.assertFalse((self.root / "audit" / "control-sessions.db").exists())

    def test_plain_user_turn_eval_uses_workspace_lock_before_provider_creation(self) -> None:
        """普通 Eval 也必须走 Runtime 门禁，不能只有控制步骤才取得工作区锁。"""

        workspace = self.root / "plain-runtime-workspace"
        workspace.mkdir()
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        case = EvalCase(
            id="plain-runtime",
            title="plain",
            task="finish only",
            source_dir=self.root,
            workspace_dir=workspace,
            verifier_dir=self.root,
            allowed_changes=(),
            required_changes=(),
            max_rounds=2,
            max_context_chars=4000,
            verifications=(),
        )
        config = AppConfig(
            workspace=workspace,
            provider=ProviderConfig(
                "openai", "synthetic", "https://example.invalid/v1", "offline"
            ),
            audit_dir=self.root / "plain-audit",
            plan_enabled=False,
        )
        provider_calls = 0

        def factory(_config: ProviderConfig, _timeout: float):  # type: ignore[no-untyped-def]
            nonlocal provider_calls
            provider_calls += 1
            return FinishingWithoutChangesProvider()

        ownership = WorkspaceLock.acquire(workspace)
        try:
            with self.assertRaisesRegex(SessionRuntimeError, "工作区正在执行其他任务"):
                _execute_case(
                    case,
                    workspace,
                    self.root / "plain-audit" / "plain.jsonl",
                    config,
                    factory,
                )
        finally:
            ownership.close()

        self.assertEqual(0, provider_calls)

    def test_eval_stops_and_preserves_database_when_runtime_cleanup_is_incomplete(self) -> None:
        """Runtime 未确认释放锁与资源时，Eval 不能删库后继续验证。"""

        workspace = self.root / "cleanup-workspace"
        workspace.mkdir()
        audit_path = self.root / "cleanup-audit" / "cleanup.jsonl"
        audit_path.parent.mkdir()
        database = audit_path.parent / "cleanup-sessions.db"
        database.write_text("synthetic-state", encoding="utf-8")
        case = EvalCase(
            id="cleanup",
            title="cleanup",
            task="finish only",
            source_dir=self.root,
            workspace_dir=workspace,
            verifier_dir=self.root,
            allowed_changes=(),
            required_changes=(),
            max_rounds=2,
            max_context_chars=4000,
            verifications=(),
        )
        config = AppConfig(
            workspace=workspace,
            provider=ProviderConfig(
                "openai", "synthetic", "https://example.invalid/v1", "offline"
            ),
            audit_dir=audit_path.parent,
            plan_enabled=False,
        )

        class IncompleteRuntime:
            def close(self) -> bool:
                return False

        scenario = RuntimeScenarioObservation(
            runtime=IncompleteRuntime(),  # type: ignore[arg-type]
            results=(RunResult(True, "done", 1),),
            runtime_instances=1,
            approval_decisions=(),
        )
        with patch(
            "tricoder.evals.service.run_runtime_scenario",
            return_value=scenario,
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                _execute_case(
                    case,
                    workspace,
                    audit_path,
                    config,
                    lambda _config, _timeout: FinishingWithoutChangesProvider(),
                )

        self.assertTrue(database.is_file())


if __name__ == "__main__":
    unittest.main()
