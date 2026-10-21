"""Regression V1 题库结构和 quality verifier 正负控制。"""

from __future__ import annotations

import argparse
from collections import Counter
import io
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from tricoder.evals.assertions import evaluate_assertions
from tricoder.evals.loader import load_experiment, load_suite
from tricoder.evals.runner import _run_verification, run_isolated_case
from tricoder.evals.service import _ExecutionObservation, _execute_case, run_eval_command
from tricoder.evals.workspace import install_verifier
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, ProviderResponse, ToolCall


_SUITE = Path(__file__).parents[1] / "evals" / "regression-v1"
_QUALITY = (
    "q-nested-create",
    "q-review-failing-tests",
    "q-history-recap",
    "q-spill-tail",
    "q-fix-regression",
    "q-memory-restart",
)
_CONTRACT = (
    "c-text-termination",
    "c-repeat-failure",
    "c-approval-denied",
    "c-restart-unchanged",
    "c-restart-changed",
    "c-baseline-corrupted",
)


class _SpillReadbackProvider:
    """只决定工具步骤；故障事实仍从真实工具输出读取。"""

    def __init__(self) -> None:
        self.calls = 0
        self.saw_tail = False

    def complete(self, messages, _tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        tool_text = "\n".join(
            message.content or "" for message in messages if message.role == "tool"
        )
        if self.calls == 1:
            return ProviderResponse(
                tool_calls=(ToolCall("read-log", "read_file", {"path": "logs/failure.log"}),),
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            match = re.search(r"spill_[0-9a-f]{32}", tool_text)
            if match is None:
                raise AssertionError("large read did not expose a spill reference")
            return ProviderResponse(
                tool_calls=(
                    ToolCall(
                        "read-tail",
                        "read_tool_result",
                        {"reference": match.group(0), "offset": 39000},
                    ),
                ),
                finish_reason="tool_calls",
            )
        self.saw_tail = (
            "EVAL_TAIL_731" in tool_text
            and "connection_pool_exhausted" in tool_text
        )
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    "finish-tail",
                    "finish",
                    {
                        "summary": (
                            "已回读日志尾部：EVAL_TAIL_731，"
                            "原因 connection_pool_exhausted。"
                        ),
                        "outcome": "completed",
                    },
                ),
            ),
            finish_reason="tool_calls",
        )


class EvalRegressionSuiteTests(unittest.TestCase):
    def test_suite_has_exactly_six_quality_and_six_contract_cases(self) -> None:
        suite = load_suite(_SUITE)

        self.assertEqual(_QUALITY + _CONTRACT, tuple(case.id for case in suite.cases))
        self.assertEqual(
            {"quality": 6, "contract": 6},
            dict(Counter(case.execution_kind for case in suite.cases)),
        )
        self.assertTrue(all(case.assertions for case in suite.cases))

    def test_experiment_templates_freeze_full_denominators(self) -> None:
        quality = load_experiment(_SUITE / "experiment-quality.toml")
        contract = load_experiment(_SUITE / "experiment-contract.toml")

        self.assertEqual(18, quality.planned_trials)
        self.assertEqual(18, quality.max_trials)
        self.assertEqual("replace-with-explicit-model", quality.conditions[0].model)
        self.assertEqual(6, contract.planned_trials)
        self.assertEqual(6, contract.max_trials)
        self.assertEqual("fake", contract.conditions[0].provider)

    def test_quality_template_placeholder_blocks_before_provider_or_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            previous = Path.cwd()
            os.chdir(temporary)
            try:
                provider_calls: list[str] = []
                output = io.StringIO()
                exit_code = run_eval_command(
                    argparse.Namespace(
                        suite=None,
                        experiment=_SUITE / "experiment-quality.toml",
                        repeat=1,
                        provider="openai",
                        model=None,
                        base_url=None,
                        env_file=None,
                        case=None,
                        dry_run=False,
                        no_color=True,
                    ),
                    environ={"OPENAI_API_KEY": "offline-placeholder"},
                    provider_factory=lambda *_args: provider_calls.append("called"),  # type: ignore[arg-type]
                    output=output,
                )

                self.assertEqual(2, exit_code)
                self.assertEqual("eval_error=configuration\n", output.getvalue())
                self.assertEqual([], provider_calls)
                self.assertFalse((Path(temporary) / "runtime" / "evals").exists())
            finally:
                os.chdir(previous)

    def test_experiment_case_filter_freezes_only_selected_repetitions(self) -> None:
        output = io.StringIO()
        exit_code = run_eval_command(
            argparse.Namespace(
                suite=None,
                experiment=_SUITE / "experiment-quality.toml",
                repeat=1,
                provider="openai",
                model=None,
                base_url=None,
                env_file=None,
                case="q-nested-create",
                dry_run=True,
                no_color=True,
            ),
            environ={},
            provider_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("dry-run must not build a Provider")
            ),
            output=output,
        )

        self.assertEqual(0, exit_code)
        self.assertIn("cases=1 trials=3", output.getvalue())
        self.assertIn("case=q-nested-create status=validated", output.getvalue())
        self.assertNotIn("case=q-memory-restart", output.getvalue())

    def test_quality_hidden_verifiers_have_positive_and_negative_controls(self) -> None:
        suite = load_suite(_SUITE)
        cases = {case.id: case for case in suite.cases}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case_id in _QUALITY:
                case = cases[case_id]
                with self.subTest(case=case_id, control="positive"):
                    workspace = root / f"{case_id}-positive"
                    shutil.copytree(case.workspace_dir, workspace)
                    self._make_positive(case_id, workspace)
                    install_verifier(case, workspace)
                    results = tuple(
                        _run_verification(spec, workspace) for spec in case.verifications
                    )
                    self.assertTrue(all(result.passed for result in results), results)

                with self.subTest(case=case_id, control="negative"):
                    workspace = root / f"{case_id}-negative"
                    shutil.copytree(case.workspace_dir, workspace)
                    self._make_negative(case_id, workspace)
                    install_verifier(case, workspace)
                    results = tuple(
                        _run_verification(spec, workspace) for spec in case.verifications
                    )
                    self.assertTrue(any(not result.passed for result in results), results)

    def test_spill_quality_smoke_uses_real_spill_and_readback_tools(self) -> None:
        """脚本 Provider 只能编排，spill 引用和尾部事实必须来自生产工具链。"""

        case = load_suite(_SUITE, case_id="q-spill-tail").cases[0]
        observed_runs: list[tuple[_SpillReadbackProvider, _ExecutionObservation]] = []
        with tempfile.TemporaryDirectory() as temporary:
            for repetition in (1, 2):
                provider = _SpillReadbackProvider()
                observation = _ExecutionObservation()
                observed_runs.append((provider, observation))
                trial = Path(temporary).resolve() / f"trial-{repetition}"
                trial.mkdir()
                config = AppConfig(
                    workspace=trial,
                    provider=ProviderConfig(
                        "openai", "offline-placeholder",
                        "https://example.invalid/v1", "offline",
                    ),
                    audit_dir=trial / "audit",
                    plan_enabled=False,
                    memory=MemoryConfig(compaction="off", persistence="off"),
                )

                result = run_isolated_case(
                    case,
                    trial,
                    lambda active_case, workspace, audit_path: _execute_case(
                        active_case,
                        workspace,
                        audit_path,
                        config,
                        lambda _provider, _timeout: provider,
                        observation=observation,
                    ),
                )
                assertions = evaluate_assertions(
                    case.assertions, observation.assertion_observation()
                )
                self.assertEqual("passed", result.status, result.failure_codes)
                self.assertTrue(provider.saw_tail)
                self.assertTrue(all(item.passed is True for item in assertions), assertions)
                self.assertEqual(
                    ["read_file", "read_tool_result", "finish"],
                    [event.name for event in observation.tool_events or ()],
                )

        self.assertEqual(2, len(observed_runs))
        self.assertIsNot(observed_runs[0][0], observed_runs[1][0])

    def test_nested_create_recovers_with_real_verification_without_changing_case(self) -> None:
        """固定原题和隐藏评分器，仅用离线 Provider 编排一次真实纠正链路。"""
        case = load_suite(_SUITE, case_id="q-nested-create").cases[0]
        owner = self

        class Provider:
            def __init__(self):
                self.calls = 0
                self.actions = [
                    ("create_file", {"path": "app/demo/main.py", "content": 'print("Hello, world!")\n', "create_parents": True}),
                    ("run_command", {"command": "python app/demo/main.py"}),
                    ("finish", {"summary": "已创建并运行"}),
                    ("run_command", {"command": "python -m compileall -q app"}),
                    ("finish", {"summary": "已创建并检查语法"}),
                ]

            def complete(self, messages, tools=()):
                if self.calls == 3:
                    owner.assertTrue(any(
                        message.kind == "verification_feedback"
                        and "missing_relevant_verification" in (message.content or "")
                        for message in messages
                    ))
                owner.assertTrue(self.actions, "不应增加隐藏的业务请求")
                name, arguments = self.actions.pop(0)
                self.calls += 1
                return ProviderResponse(tool_calls=(ToolCall(f"step-{self.calls}", name, arguments),))

        provider = Provider()
        observation = _ExecutionObservation()
        business_results = []
        with tempfile.TemporaryDirectory() as temporary:
            trial = Path(temporary).resolve()
            config = AppConfig(
                workspace=trial,
                provider=ProviderConfig("openai", "offline-placeholder", "https://example.invalid/v1", "offline"),
                audit_dir=trial / "audit", plan_enabled=False,
                memory=MemoryConfig(compaction="off", persistence="off"),
            )
            def execute(active_case, workspace, audit_path):
                business_result = _execute_case(
                    active_case, workspace, audit_path, config,
                    lambda _provider, _timeout: provider, observation=observation,
                )
                business_results.append(business_result)
                return business_result

            result = run_isolated_case(case, trial, execute)
            self.assertTrue(business_results[0].ok, business_results[0])
            self.assertEqual("通过", result.agent_verification)
            self.assertTrue(all(item.passed for item in result.verifications))
            # 原题与评分不变，宿主管理的编译缓存不应出现在工作区。
            self.assertEqual("passed", result.status, result.failure_codes)
            self.assertEqual((), result.failure_codes)
            self.assertFalse(any("/__pycache__/" in path for path in result.modified_files))
            self.assertEqual(5, provider.calls)
            self.assertTrue(all(item.passed is True for item in evaluate_assertions(
                case.assertions, observation.assertion_observation(),
            )))

    def test_nested_create_illegal_cache_remains_out_of_scope(self):
        case = load_suite(_SUITE, case_id="q-nested-create").cases[0]
        with tempfile.TemporaryDirectory() as temporary:
            def execute(active_case, workspace, audit_path):
                self._make_positive("q-nested-create", workspace)
                forbidden = workspace / "app/demo/__pycache__/extra.pyc"
                forbidden.parent.mkdir()
                forbidden.write_bytes(b"synthetic-extra")
                from tricoder.models import RunResult
                return RunResult(ok=True, summary="synthetic", rounds=1, tool_calls=1)
            result = run_isolated_case(case, Path(temporary), execute)
            self.assertEqual("failed", result.status)
            self.assertIn("change_out_of_scope", result.failure_codes)
            self.assertTrue(any("/__pycache__/" in path for path in result.modified_files))

    @staticmethod
    def _make_positive(case_id: str, workspace: Path) -> None:
        if case_id == "q-nested-create":
            target = workspace / "app" / "demo" / "main.py"
            target.parent.mkdir(parents=True)
            target.write_text('print("Hello, world!")\n', encoding="utf-8")
        elif case_id == "q-fix-regression":
            path = workspace / "formatter.py"
            path.write_text(
                'from core import join\n\n\ndef render(name: str, prefix: str = "Hi") -> str:\n'
                '    return join(prefix, name)\n',
                encoding="utf-8",
            )
        elif case_id == "q-memory-restart":
            (workspace / "memory_target.py").write_text(
                "def tri_answer():\n    return 42\n", encoding="utf-8"
            )

    @staticmethod
    def _make_negative(case_id: str, workspace: Path) -> None:
        if case_id == "q-review-failing-tests":
            path = workspace / "calculator.py"
            path.write_text(
                "def subtract(a: int, b: int) -> int:\n    return a - b\n",
                encoding="utf-8",
            )
        elif case_id == "q-history-recap":
            (workspace / "ledger.py").write_text(
                "def total(values):\n    return max(values)\n", encoding="utf-8"
            )
        elif case_id == "q-spill-tail":
            path = workspace / "logs" / "failure.log"
            path.write_text(
                path.read_text(encoding="utf-8").replace("EVAL_TAIL_731", "WRONG"),
                encoding="utf-8",
            )
        elif case_id == "q-memory-restart":
            (workspace / "memory_target.py").write_text(
                "def answer():\n    return 42\n", encoding="utf-8"
            )
        # q-nested-create 和 q-fix-regression 的原始 fixture 本身就是负例。


if __name__ == "__main__":
    unittest.main()
