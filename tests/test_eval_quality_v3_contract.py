"""Production Runtime, actual copied workspaces and scorer boundary regressions."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from delivery_helpers import QueueProvider, call, check, finish
from quality_v3_support import ROOT, SUITE, REFERENCES, cases, isolated
from tricoder.evals.faults import ControlledFaults
from tricoder.evals.service import _execute_case, _ExecutionObservation
from tricoder.execution_state import ErrorCode, RecoveryAction
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig


class QualityV3ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=cases()

    def runtime(self, case, workspace, audit, provider, observation=None):
        config=AppConfig(workspace=workspace, provider=ProviderConfig(
            "deepseek","offline-synthetic","https://example.invalid","offline"),
            audit_dir=audit.parent, plan_enabled=False,
            memory=MemoryConfig(compaction="off",persistence="off"))
        return _execute_case(case,workspace,audit,config,lambda *_:provider,
                             observation=observation)

    def test_docs_publish_version_exposure_and_limitations(self):
        self.assertTrue((SUITE/"README.md").is_file(), "V3 public methodology missing")
        readme=(SUITE/"README.md").read_text("utf8")
        for fact in ("30", "22", "8", "16", "16000", "未运行真实模型", "不可直接"):
            self.assertIn(fact,readme)
        self.assertNotIn("待Task", (SUITE/"migration.md").read_text("utf8"))

    def test_real_score_tests_only_forbidden_and_nested_file_changes(self):
        case=self.cases["sf-subtract"]
        correct=REFERENCES[case.id]["correct"]
        for label,files,expected in (
            ("tests-only",{"tests/test_added.py":"# tests alone\n"},("verification_failed","required_change_missing")),
            ("outside",{**correct,"forbidden.py":"x=1\n"},("change_out_of_scope",)),
            ("nested",{**correct,"tests/tests/test_probe.py":"# nested\n"},("change_out_of_scope",)),
            ("allowed-tests",{**correct,"tests/test_added.py":"# allowed\n"},()),
        ):
            with self.subTest(label=label):
                result,_,_=isolated(case,label,files)
                self.assertEqual(expected,result.failure_codes)
                self.assertEqual(tuple(sorted(files)),result.modified_files)
                if label!="tests-only": self.assertTrue(all(v.passed for v in result.verifications))

    def test_correct_artifact_without_finish_is_agent_failed(self):
        case=self.cases["sf-subtract"]; observation=_ExecutionObservation()
        def execute(c,workspace,audit):
            old=(workspace/"math_ops.py").read_text("utf8")
            actions=[[call("read_file",path="math_ops.py")],
                     [call("edit_file",path="math_ops.py",old_text=old,new_text=REFERENCES[c.id]["correct"]["math_ops.py"])],
                     check("python -m unittest discover -s tests -v"),
                     check("python -m compileall -q math_ops.py")]
            actions += [[call("read_file",path="math_ops.py")]] * (c.max_rounds-len(actions))
            provider=QueueProvider(actions)
            result=self.runtime(c,workspace,audit,provider,observation)
            self.assertLessEqual(result.rounds,16)
            self.assertFalse(result.ok)
            return result
        result,_,_=isolated(case,"runtime-no-finish",{},executor=execute)
        self.assertEqual(("agent_failed","agent_unverified"),result.failure_codes)
        self.assertTrue(all(v.passed for v in result.verifications))
        self.assertEqual(("math_ops.py",),result.modified_files)
        self.assertNotEqual("completed",observation.turn_results[0].stop_reason)
        self.save("no-finish-turns",observation.turn_results)

    def partial(self,fail_first):
        case=self.cases["mm-partial-revision"]; observation=_ExecutionObservation()
        first="def render(text,prefix):\n    return prefix+text.strip()\n"
        snapshots=[]
        def execute(c,workspace,audit):
            initial=(workspace/"formatter.py").read_text("utf8")
            reads=[call("read_file",path=p) for p in ("README.md","tests/test_public.py","formatter.py")]
            actions=[reads,
                     [call("edit_file",path="formatter.py",old_text=initial,new_text=first)],
                     check("python -m unittest discover -s tests -v"),
                     check("python -m compileall -q formatter.py"),
                     finish("incomplete" if fail_first else "completed"),
                     [call("edit_file",path="formatter.py",old_text=first,new_text=REFERENCES[c.id]["correct"]["formatter.py"])],
                     check("python -m unittest discover -s tests -v"),
                     check("python -m compileall -q formatter.py"),finish()]
            provider=QueueProvider(actions)
            def inspect(number):
                self.assertFalse((workspace/".tricoder_eval_verifier").exists())
                self.assertFalse(any(p.name=="quality_v3_support.py" for p in workspace.rglob("*")))
                history=provider.histories[-1]
                if number<=5:
                    visible="\n".join(str(m.content) for m in history)
                    for future in ("撤回prefix","忽略其值","strip后再lower"):
                        self.assertNotIn(future,visible)
                    self.assertNotIn("wrong_a",visible)
                if number==6:
                    p=subprocess.run([sys.executable,"-B","-c",
                        "from formatter import render; print(render(' MiXeD ','P:'))"],
                        cwd=workspace,capture_output=True,text=True)
                    self.assertEqual((0,"P:MiXeD\n"),(p.returncode,p.stdout),p.stderr)
                    snapshots.append(dict(phase="after_first_turn",output=p.stdout,exit_code=p.returncode))
            provider.before_response=inspect
            try:
                result=self.runtime(c,workspace,audit,provider,observation)
            except Exception:
                self.save("debug-partial-"+str(fail_first),provider.histories[-1])
                raise
            self.assertFalse(provider.actions)
            self.assertEqual(2,len(observation.turn_results))
            self.assertEqual((not fail_first,True),tuple(t.ok for t in observation.turn_results))
            self.assertEqual(not fail_first,result.ok)
            return result
        label="runtime-and-negative" if fail_first else "runtime-two-turns"
        result,_,_=isolated(case,label,{},executor=execute)
        self.assertEqual(("agent_failed",) if fail_first else (), result.failure_codes)
        self.assertTrue(all(v.passed for v in result.verifications))
        self.assertEqual(("formatter.py",),result.modified_files)
        self.assertEqual(1,len(snapshots))
        self.save(label+"-turns",observation.turn_results)

    def test_two_turn_runtime_preserves_then_revises_real_behavior(self):
        self.partial(False)

    def test_first_turn_failed_last_turn_success_does_not_erase_failure(self):
        self.partial(True)

    def test_memory_first_messages_and_visible_files_do_not_contain_corrections(self):
        for id in ("mm-early-constraint","mm-latest-correction","mm-todo-once"):
            case=self.cases[id]
            def execute(c,workspace,audit):
                read=[call("read_file",path=p) for p in ("README.md","tests/test_public.py")]
                provider=QueueProvider([read,finish(),finish("incomplete")])
                observation=_ExecutionObservation()
                def inspect(number):
                    self.assertFalse((workspace/".tricoder_eval_verifier").exists())
                    self.assertEqual([],list(workspace.rglob("*support*")))
                    if number==2:
                        visible="\n".join(str(m.content) for m in provider.histories[-1])
                        self.assertNotIn(c.steps[1].content,visible)
                        self.assertNotIn("Behavior.test_boundary",visible)
                        if id=="mm-latest-correction":
                            for phrase in ("始终选择latest","包含空字符串","不因假值回退"):
                                self.assertNotIn(phrase,visible)
                        tool_messages=[m for m in provider.histories[-1] if m.role=="tool"]
                        self.assertEqual(2,len(tool_messages))  # actually read, not just inspect task text
                provider.before_response=inspect
                result=self.runtime(c,workspace,audit,provider,observation)
                self.assertTrue(observation.turn_results[0].ok)
                self.assertFalse(provider.actions)
                return result
            isolated(case,"runtime-memory-isolation",{},executor=execute)

    def test_truncation_read_search_glob_and_provider_counter_migration(self):
        case=self.cases["rc-search-truncated"]
        before={p.relative_to(case.source_dir).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in case.source_dir.rglob("*") if p.is_file()}
        for name in ("read_file","search_text","glob_files"):
            faults=ControlledFaults(case.faults)
            result=faults.before_tool(name)
            self.assertFalse(result.ok)
            self.assertEqual(ErrorCode.OUTPUT_LIMIT,result.error.code)
            self.assertEqual(RecoveryAction.REPLAN,result.error.recovery)
            self.assertIsNone(faults.before_tool(name))
            self.assertEqual(1,faults.tool_replans)
        self.assertEqual(before,{p.relative_to(case.source_dir).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in case.source_dir.rglob("*") if p.is_file()})
        clean=ControlledFaults(self.cases["rc-provider-transient"].faults); clean.before_provider()
        self.assertFalse(clean.fault_triggered); self.assertEqual(0,clean.provider_retries)
        old=ControlledFaults(("provider_transient",)); old.before_provider()
        self.assertEqual(1,old.provider_retries)

    def test_cache_service_cannot_bypass_public_get(self):
        case=self.cases["cf-cache-copy-isolation"]
        files={**REFERENCES[case.id]["correct"],
               "service.py":"from copy import deepcopy\n\ndef load_record(cache,key):\n    return deepcopy(cache._data[key])\n"}
        result,_,_=isolated(case,"service-bypasses-get",files)
        self.assertEqual(("verification_failed",),result.failure_codes)
        self.assertEqual(1,result.verifications[0].exit_code)

    def save(self,label,turns):
        target=os.environ.get("TRICODER_V3_EVIDENCE")
        if target:
            path=Path(target)/(label+".json")
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open("x",encoding="utf8") as stream:
                json.dump([asdict(t) for t in turns],stream,ensure_ascii=False,indent=2)
