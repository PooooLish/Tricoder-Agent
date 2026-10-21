"""逐轮失败保留、真实 Runtime 接线与故障证据的安全往返。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from tricoder.evals import models
from tricoder.evals.faults import ControlledFaults
from tricoder.evals.loader import load_suite
from tricoder.evals.models import EvalCondition, PlannedTrial, ScenarioStep, TrialKey, TrialRecord, TrialDimensions, ExperimentRunReport
from tricoder.evals.report import trial_record_as_dict, trial_record_from_dict, experiment_report_as_dict, _render_experiment_markdown
from tricoder.evals.runner import EvalCaseResult
from tricoder.evals.service import _ExecutionObservation, _EvalAgentObserver, _execute_case, _trial_from_case_result
from tricoder.execution_state import ErrorCode, RecoveryAction
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, ProviderResponse, ToolCall, RunResult, tool_failure

ROOT = Path(__file__).parents[1]


def multi_trial_payload():
    """One shared case, two conditions, repetitions, and a legacy diagnostics row."""
    records = (
        TrialRecord(TrialKey("alpha", "sf-subtract", 1), "quality", "failed",
            TrialDimensions(agent_completed=False), failure_codes=("agent_failed",),
            turn_results=(models.EvalTurnRecord(1, False, "not-run", 1, 1, "policy_denied"),
                          models.EvalTurnRecord(2, True, "passed", 2, 2)), first_failed_turn=1,
            triggered_faults=("provider_transient",), fault_evidence=(("provider_transient", "counter_only"),)),
        TrialRecord(TrialKey("alpha", "sf-subtract", 2), "quality", "passed",
            TrialDimensions(agent_completed=True),
            turn_results=(models.EvalTurnRecord(1, True, "passed", 3, 3),)),
        TrialRecord(TrialKey("beta", "sf-subtract", 1), "quality", "failed",
            TrialDimensions(agent_completed=False), failure_codes=("agent_failed",),
            triggered_faults=("provider_transient",)),
    )
    report = ExperimentRunReport("offline-example", "multi-trial", "quality-v2", "2026-10-19T00:00:00+00:00", 3, 1, True, records)
    payload = experiment_report_as_dict(report)
    for field in ("turn_results", "first_failed_turn", "fault_evidence"):
        payload["trials"][2].pop(field)
    return payload


def diagnostic_table(markdown, section):
    content = markdown.split("## " + section + "\n", 1)[1].split("\n## ", 1)[0]
    return [[cell.strip() for cell in line.strip("|").split("|")]
            for line in content.splitlines() if line.startswith("|")]


class EvalDiagnosticsTests(unittest.TestCase):
    def test_delivery_optional_fields_round_trip_old_unknown_and_strict_types(self):
        payload = multi_trial_payload()['trials'][0]
        new = dict(finish_outcome='incomplete', stop_reason='model_incomplete',
                   validation_status='stale', unresolved_check_count=1, verification_required=True)
        payload['turn_results'][0].update(new)
        record = trial_record_from_dict(payload)
        self.assertEqual(new, {k: getattr(record.turn_results[0], k, None) for k in new})
        self.assertEqual(record, trial_record_from_dict(trial_record_as_dict(record)))
        for k in new:
            payload['turn_results'][0].pop(k)
        old = trial_record_from_dict(payload)
        for k in new:
            self.assertIsNone(getattr(old.turn_results[0], k))
        for field, values in {
            'finish_outcome': ['PRIVATE|' * 2000, 1, []],
            'stop_reason': ['PRIVATE|' * 2000, False, {}],
            'validation_status': ['PRIVATE', 1, []],
            'unresolved_check_count': [True, -1, '1', 0.5],
            'verification_required': [1, 0, 'false'],
        }.items():
            for value in values:
                with self.subTest(field=field, value=type(value)), self.assertRaises(ValueError):
                    modified = json.loads(json.dumps(payload))
                    modified['turn_results'][0][field] = value
                    trial_record_from_dict(modified)

    def test_delivery_table_every_row_identity_and_unknown(self):
        payload = multi_trial_payload()
        payload['trials'][0]['turn_results'][0].update(finish_outcome='incomplete',
            stop_reason='model_incomplete', validation_status='unverified',
            unresolved_check_count=0, verification_required=False)
        rows = diagnostic_table(_render_experiment_markdown(payload), 'Delivery diagnostics')[2:]
        self.assertEqual([
            ['alpha','sf-subtract','1','1','incomplete','False','model_incomplete','unverified','0','False'],
            ['alpha','sf-subtract','1','2','unknown','True','unknown','unknown','unknown','unknown'],
            ['alpha','sf-subtract','2','1','unknown','True','unknown','unknown','unknown','unknown'],
            ['beta','sf-subtract','1','-','unknown','unknown','unknown','unknown','unknown','unknown'],
        ], rows)

    def test_real_runtime_delivery_turns_keep_and_and_reset_declaration(self):
        from delivery_helpers import QueueProvider, ORIGINAL, edit, check, finish, call, reconstructed
        scenarios = [
            ([finish(), edit(), check(), finish()], [True, True], ['completed', 'completed'], ['completed', 'completed']),
            ([[call('ask_user', question='more?')], edit(), check(), finish()], [False, True], [None, 'completed'], ['needs_input','completed']),
            ([finish()] + reconstructed() + [finish(),finish()], [True,False], ['completed','completed'], ['completed','verification_blocked']),
            ([finish(), [call('ask_user', question='more?')]], [True,False], ['completed',None], ['completed','needs_input']),
        ]
        for actions, oks, declarations, reasons in scenarios:
            with self.subTest(oks=oks, reasons=reasons), tempfile.TemporaryDirectory(dir=ROOT/'runtime', prefix='delivery-eval-') as temporary:
                root = Path(temporary); workspace = root/'workspace'; workspace.mkdir()
                (workspace/'config.py').write_text(ORIGINAL, encoding='utf8')
                case = replace(self.case, max_rounds=16, steps=(ScenarioStep('user_turn','只确认'),ScenarioStep('user_turn','修复并验证')))
                config = AppConfig(workspace=workspace, provider=ProviderConfig('deepseek','synthetic','https://example.invalid','offline'),
                    plan_enabled=False, memory=MemoryConfig(compaction='off',persistence='off'))
                obs = _ExecutionObservation(); provider = QueueProvider(actions)
                result = _execute_case(case,workspace,root/'audit/run.jsonl',config,lambda *_:provider,observation=obs)
                self.assertEqual(all(oks), result.ok)
                self.assertEqual(oks,[t.ok for t in obs.turn_results])
                self.assertEqual(declarations,[t.finish_outcome for t in obs.turn_results])
                self.assertEqual(reasons,[t.stop_reason for t in obs.turn_results])
                self.assertEqual(0,obs.turn_results[0].unresolved_check_count)
                self.assertFalse(obs.turn_results[0].verification_required)

    def render_multi_trial(self):
        payload = multi_trial_payload()
        before = json.dumps(payload, sort_keys=True)
        keys = [trial_record_from_dict(row).key for row in payload["trials"]]
        markdown = _render_experiment_markdown(payload)
        self.assertEqual(before, json.dumps(payload, sort_keys=True))
        self.assertEqual(keys, [trial_record_from_dict(row).key for row in payload["trials"]])
        return markdown

    def test_each_user_turn_and_legacy_unknown_row_has_full_trial_identity(self):
        table = diagnostic_table(self.render_multi_trial(), "User turns")
        self.assertEqual([
            ["alpha", "sf-subtract", "1", "1", "False", "not-run", "1", "1", "policy_denied"],
            ["alpha", "sf-subtract", "1", "2", "True", "passed", "2", "2", "None"],
            ["alpha", "sf-subtract", "2", "1", "True", "passed", "3", "3", "None"],
            ["beta", "sf-subtract", "1", "-", "unknown", "unknown", "-", "-", "unknown"],
        ], table[2:])
        self.assertEqual(["Condition", "Case", "Repetition", "Turn", "OK", "Verification", "Rounds", "Tools", "Terminal tool error"], table[0])
        for row in table:
            self.assertEqual(9, len(row), row)

    def test_each_fault_row_has_identity_and_no_untriggered_row_is_invented(self):
        table = diagnostic_table(self.render_multi_trial(), "Fault evidence")
        self.assertEqual([
            ["alpha", "sf-subtract", "1", "provider_transient", "counter_only"],
            ["beta", "sf-subtract", "1", "provider_transient", "证据类型未知"],
        ], table[2:])
        self.assertEqual(["Condition", "Case", "Repetition", "Triggered fault", "Evidence kind"], table[0])
        for row in table:
            self.assertEqual(5, len(row), row)

    def test_diagnostic_identity_still_uses_validated_trial_keys(self):
        for field in ("condition_id", "case_id", "repetition"):
            payload = multi_trial_payload()
            payload["trials"][0][field] = "SYNTHETIC_PRIVATE_MARKER|injected\nrow"
            with self.subTest(field=field), self.assertRaises(ValueError):
                _render_experiment_markdown(payload)

    def setUp(self):
        self.case = load_suite(ROOT / "evals/quality-v2").cases[0]
        condition = EvalCondition("baseline", "deepseek", "offline", "quality", "off", "off", ("hidden_verifier",), ("none",))
        self.plan = PlannedTrial(0, TrialKey("baseline", self.case.id, 1), self.case, condition)

    def record(self, observation, ok=True):
        result = EvalCaseResult(self.case.id, "passed" if ok else "failed", () if ok else ("agent_failed",),
            1, 2, 2, (), (), "未运行", None)
        return _trial_from_case_result(self.plan, result, observation=observation)

    def test_actual_runtime_keeps_failed_then_successful_turn_and_root_error(self):
        observation = _ExecutionObservation()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); workspace = root / "workspace"; workspace.mkdir()
            case = replace(self.case, task="unused", steps=(ScenarioStep("user_turn", "first"), ScenarioStep("user_turn", "second")))
            config = AppConfig(workspace=workspace, provider=ProviderConfig("deepseek", "synthetic", "https://example.invalid/v1", "offline"),
                audit_dir=root / "audit", plan_enabled=False, memory=MemoryConfig(compaction="off", persistence="off"))
            class Provider:
                def __init__(self): self.calls = 0
                def complete(self, messages, tools=()):
                    self.calls += 1
                    call = ToolCall("deny", "run_command", {"command": "python -c pass"}) if self.calls == 1 else ToolCall("finish", "finish", {"summary": "done"})
                    return ProviderResponse(tool_calls=(call,), finish_reason="tool_calls")
            provider = Provider()
            result = _execute_case(case, workspace, root / "audit/run.jsonl", config, lambda *_: provider, observation=observation)
        self.assertFalse(result.ok)
        record = self.record(observation, result.ok)
        self.assertEqual(2, len(getattr(record, "turn_results", ())), "逐轮失败事实丢失")
        self.assertEqual([False, True], [t.ok for t in record.turn_results])
        self.assertEqual(1, record.first_failed_turn)
        self.assertEqual("policy_denied", record.turn_results[0].terminal_tool_error)
        self.assertIsNone(record.turn_results[1].terminal_tool_error)
        self.assertEqual(("agent_failed",), record.failure_codes)

    def test_recoverable_error_and_unknown_failure_do_not_invent_terminal_error(self):
        observation = _ExecutionObservation()
        callback = getattr(observation, "on_user_turn_completed", None)
        self.assertTrue(callable(callback), "需要每轮结束的固定结果观察回调")
        observer = _EvalAgentObserver(observation)
        observer.on_tool_result(ToolCall("read", "read_file", {}), tool_failure(ErrorCode.EXECUTION_FAILED, "SYNTHETIC_PRIVATE_MARKER", recovery=RecoveryAction.REPLAN), 1)
        callback(1, RunResult(True, "secret", 2, 2))
        callback(2, RunResult(False, "policy_denied in untrusted summary", 1, 0))
        record = self.record(observation, False)
        self.assertEqual([None, None], [t.terminal_tool_error for t in record.turn_results])
        self.assertEqual(2, record.first_failed_turn)
        single = self.record(replace(observation, turn_results=observation.turn_results[:1]))
        self.assertIsNone(single.first_failed_turn)

    def test_triggered_faults_distinguish_counter_and_synthetic_results(self):
        faults = ControlledFaults(("provider_transient", "tool_invalid_args", "tool_transient_read"))
        observation = _ExecutionObservation(faults=faults)
        untouched = self.record(observation)
        self.assertIsNone(untouched.dimensions.recovered)
        self.assertIn("fault_evidence", trial_record_as_dict(untouched))
        self.assertEqual((), untouched.fault_evidence)
        faults.before_provider(); faults.before_tool("edit_file"); faults.before_tool("read_file")
        record = self.record(observation)
        self.assertEqual({"provider_transient": "counter_only", "tool_invalid_args": "synthetic_tool_result", "tool_transient_read": "synthetic_tool_result"}, dict(record.fault_evidence))
        self.assertEqual(3, record.retries)
        self.assertEqual(record, trial_record_from_dict(trial_record_as_dict(record)))

    def test_round_trip_markdown_old_rows_and_unsafe_diagnostics(self):
        cls = getattr(models, "EvalTurnRecord", None)
        self.assertIsNotNone(cls, "需要受限的逐轮记录类型")
        record = TrialRecord(self.plan.key, "quality", "failed", TrialDimensions(agent_completed=False),
            turn_results=(cls(1, False, "not-run", 1, 1, "policy_denied"), cls(2, True, "passed", 2, 2)), first_failed_turn=1,
            triggered_faults=("provider_transient",), fault_evidence=(("provider_transient", "counter_only"),))
        payload = trial_record_as_dict(record)
        self.assertEqual(record, trial_record_from_dict(json.loads(json.dumps(payload))))
        report = ExperimentRunReport("test", "baseline", "quality-v2", "2026-10-19T00:00:00+00:00", 1, 1, True, (record,))
        rendered = _render_experiment_markdown(experiment_report_as_dict(report))
        self.assertIn("policy_denied", rendered)
        self.assertIn("counter_only", rendered)
        self.assertIn("模拟", rendered)
        for field in ("turn_results", "first_failed_turn", "fault_evidence"):
            payload.pop(field)
        old = trial_record_from_dict(payload)
        self.assertEqual((), old.turn_results)
        self.assertIsNone(old.first_failed_turn)
        self.assertIn("证据类型未知", _render_experiment_markdown(experiment_report_as_dict(replace(report, trials=(old,)))))
        for fault in ({"fault_id": "SYNTHETIC_PRIVATE_MARKER", "kind": "counter_only"}, {"fault_id": "provider_transient", "kind": "actual_http_retry"}):
            malicious = dict(payload, fault_evidence=[fault])
            with self.assertRaises(ValueError): trial_record_from_dict(malicious)
        for field, value in (("terminal_tool_error", "SYNTHETIC_PRIVATE_MARKER"), ("verification", "SYNTHETIC_PRIVATE_MARKER"), ("ok", 1), ("rounds", -1)):
            turn = dict(turn_index=1, ok=False, verification="not-run", rounds=1, tool_calls=1, terminal_tool_error=None)
            turn[field] = value
            with self.assertRaises(ValueError): trial_record_from_dict(dict(payload, turn_results=[turn], first_failed_turn=1))
