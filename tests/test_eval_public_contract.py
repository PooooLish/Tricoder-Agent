"""公开范围必须经过真实 Eval/Runtime 接线，且不提前注入后续需求。"""
from dataclasses import replace
import importlib
from pathlib import Path
import shutil
import tempfile
import unittest

from tricoder.evals.loader import load_suite, EvalDefinitionError
from tricoder.evals.models import EvalCase, ScenarioStep
from tricoder.evals.service import _execute_case
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, ProviderResponse, ToolCall

ROOT = Path(__file__).parents[1]


class EvalPublicContractTests(unittest.TestCase):
    def test_root_scope_sentence_is_explicit_and_legacy_text_is_unchanged(self):
        case = load_suite(ROOT / "evals/quality-v2").cases[0]
        sentence = "上述路径模式均从工作区根匹配；通配符只匹配单层名称，不包含任意父目录前缀。"
        for step in self.build(case):
            if step.kind == "user_turn":
                self.assertEqual(1, step.content.count(sentence))
        legacy = replace(case, path_match_mode="legacy")
        for old, new in zip(self.build(legacy), self.build(case)):
            self.assertEqual(old.content, new.content.replace(sentence, ""))
        self.assertEqual(case.steps, self.build(replace(case, expose_edit_scope=False)))

    def build(self, case):
        self.assertIn("expose_edit_scope", EvalCase.__dataclass_fields__, "缺少可选公开契约字段")
        return importlib.import_module("tricoder.evals.prompts").build_case_steps(case)

    def test_default_keeps_steps_and_task_byte_for_byte(self):
        case = load_suite(ROOT / "evals/quality-v1").cases[0]
        self.assertEqual(case.steps, self.build(case))
        self.assertEqual((ScenarioStep("user_turn", content=case.task),), self.build(replace(case, steps=())))

    def test_each_user_turn_has_scope_but_controls_and_later_correction_stay_in_place(self):
        case = next(c for c in load_suite(ROOT / "evals/quality-v1").cases if c.id == "mm-latest-correction")
        self.build(case)
        control = ScenarioStep("memory_refresh")
        case = replace(case, expose_edit_scope=True, allowed_changes=("config.py", "tests", "tests/*.py"),
                       steps=(case.steps[0], control, case.steps[1]))
        steps = self.build(case)
        self.assertEqual(3, len(steps))
        self.assertIs(control, steps[1])
        self.assertNotIn("latest", steps[0].content)
        for step in (steps[0], steps[2]):
            for allowed in case.allowed_changes:
                self.assertIn(allowed, step.content)
            self.assertNotIn("test_empty_latest", step.content)
            self.assertNotIn(".tricoder_eval_verifier", step.content)
        fallback = self.build(replace(case, steps=()))
        self.assertEqual(1, len(fallback))
        self.assertTrue(fallback[0].content.startswith(case.task))

    def test_loader_accepts_only_real_bool_in_schema_two(self):
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "suite"
            shutil.copytree(ROOT / "evals/quality-v1", suite, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = suite / "cases/sf-subtract/case.toml"
            original = target.read_text(encoding="utf8")
            for raw in ('true', 'false', '1', '"true"'):
                target.write_text("expose_edit_scope = " + raw + "\n" + original, encoding="utf8")
                if raw in ('true', 'false'):
                    try:
                        case = load_suite(suite).cases[0]
                    except EvalDefinitionError as exc:
                        self.fail(f"合法 bool 字段被拒绝: {exc}")
                    self.assertEqual(raw == 'true', case.expose_edit_scope)
                else:
                    with self.assertRaises(EvalDefinitionError):
                        load_suite(suite)
            legacy = Path(temporary) / "legacy"
            shutil.copytree(ROOT / "evals/smoke", legacy)
            target = next(legacy.glob("cases/*/case.toml"))
            target.write_text("expose_edit_scope = true\n" + target.read_text(encoding="utf8"), encoding="utf8")
            with self.assertRaises(EvalDefinitionError):
                load_suite(legacy)

    def test_real_eval_runtime_provider_receives_each_public_user_turn(self):
        base = load_suite(ROOT / "evals/quality-v1").cases[0]
        self.build(base)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf8")
            case = replace(base, task="NEVER_INSERT_LATER", steps=(ScenarioStep("user_turn", "first"),
                ScenarioStep("approve"), ScenarioStep("user_turn", "later correction")),
                expose_edit_scope=True, allowed_changes=("app.py", "tests", "tests/*.py"))
            histories = []
            class Provider:
                def complete(self, messages, tools=()):
                    histories.append(list(messages))
                    return ProviderResponse(tool_calls=(ToolCall("finish", "finish", {"summary": "done"}),), finish_reason="tool_calls")
            config = AppConfig(workspace=workspace, provider=ProviderConfig("deepseek", "synthetic", "https://example.invalid/v1", "offline"),
                audit_dir=root / "audit", plan_enabled=False, memory=MemoryConfig(compaction="off", persistence="off"))
            result = _execute_case(case, workspace, root / "audit/test.jsonl", config, lambda *_: Provider())
            self.assertTrue(result.ok, result)
            self.assertEqual(2, len(histories))
            for index, history in enumerate(histories):
                users = [m.content for m in history if m.role == "user"]
                current = [m.content for m in history if m.kind == "task"][-1]
                self.assertIn("tests/*.py", current)
                self.assertNotIn("NEVER_INSERT_LATER", "\n".join(users))
                if index == 0:
                    self.assertNotIn("later correction", "\n".join(users))
