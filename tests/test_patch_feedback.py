"""受限补丁的公开示例、脱敏诊断及真实 Agent 恢复/停止边界。"""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.execution_state import ErrorCode, RecoveryAction, ToolError, FileEffects, EffectState, should_stop_task
from tricoder.models import MemoryConfig, ProviderResponse, ToolCall, tool_failure
from tricoder.patches import PatchError, apply_file_patch, parse_unified_diff
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.write import ApplyPatchTool
from tests.test_agent_termination import StrictQueueProvider


VALID = "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-value = 1\n+value = 2\n"
PRIVATE = "SYNTHETIC_PRIVATE_MARKER"
DETAILS = ("git_workspace_unavailable", "patch_invalid_header", "patch_invalid_hunk",
           "patch_count_mismatch", "patch_unsupported_operation", "patch_context_mismatch", "patch_invalid_format")


def response(name, arguments):
    return ProviderResponse(tool_calls=(ToolCall(name, name, arguments),), finish_reason="tool_calls")


class PatchFeedbackTests(unittest.TestCase):
    def test_count_mismatch_reports_only_determined_bounded_counts(self):
        bad = VALID.replace("-1,1 +1,1", "-1,2 +1,2").replace("app.py", PRIVATE)
        for prefix, index in (("", 1), (VALID, 2)):
            with self.subTest(index=index), self.assertRaises(PatchError) as raised:
                parse_unified_diff(prefix + bad)
            expected = {"hunk_index": index, "declared_old": 2, "declared_new": 2,
                        "observed_old": 1, "observed_new": 1}
            self.assertEqual(expected, dict(getattr(raised.exception, "diagnostics", ())))
            result = self.registry.execute("apply_patch", {"patch": prefix + bad})
            for key, value in expected.items():
                self.assertIn(f"{key}={value}", result.output)
            self.assertNotIn(PRIVATE, result.output)
            self.assertNotIn("value =", result.output)
            self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertEqual([], self.approvals)
        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))

    def test_unknown_patch_counts_are_omitted_and_diagnostics_are_whitelisted(self):
        for source in ("invalid " + PRIVATE, VALID + "+extra\n"):
            with self.assertRaises(PatchError) as raised:
                parse_unified_diff(source)
            self.assertEqual((), getattr(raised.exception, "diagnostics", None))
        self.assertEqual((), getattr(PatchError("legacy"), "diagnostics", None))
        for diagnostics in ((("path", 1),), (("declared_old", True),), (("observed_new", -1),),
                            (("observed_old", 1_000_001),), (("hunk_index", PRIVATE),),
                            (("hunk_index", 1), ("hunk_index", 2)), []):
            with self.subTest(diagnostics=diagnostics), self.assertRaises(ValueError) as raised:
                PatchError("fixed", diagnostics=diagnostics)
            self.assertNotIn(PRIVATE, str(raised.exception))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf8")
        self.approvals = []
        self.registry = ToolRegistry(ToolContext(WorkspacePolicy(self.workspace), CommandPolicy(), self.approve))

    def approve(self, *args):
        self.approvals.append(args)
        return True

    def test_old_contract_and_whitelist_detail_do_not_grant_recovery(self):
        old = ToolError(ErrorCode.INVALID_ARGUMENT, RecoveryAction.REPLAN, False)
        self.assertEqual({"code": "invalid_argument", "recovery": "replan", "retryable": False}, old.public_fields())
        self.assertIsNone(getattr(old, "detail_code", "missing"), "旧构造必须有默认空诊断")
        for detail in DETAILS:
            result = tool_failure(ErrorCode.POLICY_DENIED, "fixed", detail_code=detail)
            self.assertEqual(detail, result.error.public_fields()["detail_code"])
            self.assertTrue(should_stop_task(result.error, FileEffects(EffectState.NONE)))
        recoverable = ToolError(ErrorCode.INVALID_ARGUMENT, RecoveryAction.REPLAN, detail_code="patch_invalid_header")
        self.assertFalse(should_stop_task(recoverable, FileEffects(EffectState.NONE)))
        self.assertTrue(should_stop_task(recoverable, FileEffects(EffectState.UNKNOWN)))
        for invalid in ("", PRIVATE, "patch_context_mismatch\n" + PRIVATE, 1, [], {}):
            with self.subTest(value=type(invalid).__name__), self.assertRaises(ValueError) as raised:
                ToolError(ErrorCode.INVALID_ARGUMENT, RecoveryAction.REPLAN, detail_code=invalid)
            self.assertNotIn(PRIVATE, str(raised.exception))

    def test_public_example_is_valid_and_documents_actual_grammar(self):
        description = ApplyPatchTool.description
        found = re.search(r"--- a/[^\n]+\n\+\+\+ b/[^\n]+\n@@[^\n]+\n(?:[ +\-][^\n]*\n)+", description)
        self.assertIsNotNone(found, "工具说明缺少完整可执行 unified diff 示例")
        parsed = parse_unified_diff(found.group())[0]
        original = "".join(line[1:] for hunk in parsed.hunks for line in hunk.lines if line[0] in " -")
        updated = apply_file_patch(original, parsed)
        self.assertNotEqual(original, updated)
        for phrase in ("真实换行", "上下文", "Markdown", "Begin/End Patch"):
            self.assertIn(phrase, description)

    def test_structural_errors_have_fixed_detail_and_never_echo_patch_body(self):
        cases = {
            "patch_invalid_header": "+++ b/app.py\n" + PRIVATE,
            "patch_invalid_hunk": VALID.replace("@@ -1,1 +1,1 @@", "@@ bad @@"),
            "patch_count_mismatch": VALID.replace("-1,1", "-1,2"),
            "patch_unsupported_operation": VALID.replace("+++ b/app.py", "+++ /dev/null"),
        }
        for expected, source in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(PatchError) as raised:
                    parse_unified_diff(source)
                self.assertEqual(expected, getattr(raised.exception, "detail_code", None))
                result = self.registry.execute("apply_patch", {"patch": source})
                self.assertEqual(expected, result.error.detail_code)
                self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
                self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
                self.assertNotIn(PRIVATE, result.output)
                self.assertEqual((), result.modified_paths)
                self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertEqual([], self.approvals)
        self.assertEqual("patch_invalid_format", getattr(PatchError("old constructor"), "detail_code", None))
        with self.assertRaises(ValueError):
            PatchError("fixed", detail_code=PRIVATE)

    def test_context_mismatch_preflights_every_file_without_partial_write_or_approval(self):
        (self.workspace / "other.py").write_text("actual = 1\n", encoding="utf8")
        bad_second = VALID.replace("app.py", "other.py").replace("value = 1", PRIVATE)
        result = self.registry.execute("apply_patch", {"patch": VALID + bad_second})
        self.assertFalse(result.ok)
        self.assertEqual("patch_context_mismatch", getattr(result.error, "detail_code", None))
        self.assertEqual(ErrorCode.EXECUTION_FAILED, result.error.code)
        self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
        self.assertEqual((), result.modified_paths)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertNotIn(PRIVATE, result.output)
        self.assertEqual([], self.approvals)
        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))
        self.assertEqual("actual = 1\n", (self.workspace / "other.py").read_text(encoding="utf8"))

    def test_real_agent_recovers_and_protocol_audit_only_retain_safe_diagnostic(self):
        provider = StrictQueueProvider([
            response("apply_patch", {"patch": "*** Begin Patch\n" + PRIVATE + "\n*** End Patch"}),
            response("apply_patch", {"patch": VALID}),
            response("run_command", {"command": "python -m compileall -q app.py"}),
            response("finish", {"summary": "done"}),
        ])
        audit_path = self.root / "audit.jsonl"
        result = CodingAgent(provider, self.registry, max_rounds=4, plan_enabled=False,
            audit=AuditLogger(audit_path), memory_config=MemoryConfig(compaction="off", persistence="off")).run("update and verify")
        self.assertTrue(result.ok, result)
        feedback = [m.content for m in provider.histories[1] if m.role == "tool"]
        self.assertEqual(1, len(feedback))
        self.assertIn("patch_invalid_header", feedback[0])
        self.assertNotIn(PRIVATE, feedback[0])
        self.assertEqual("通过", result.verification)
        self.assertEqual(("app.py",), result.modified_files)
        audit_text = audit_path.read_text(encoding="utf8")
        self.assertIn("patch_invalid_header", audit_text)
        self.assertNotIn(PRIVATE, audit_text)
        self.assertNotIn(VALID, audit_text)
        records = [json.loads(line) for line in audit_text.splitlines()]
        self.assertTrue(any("detail_code" in json.dumps(record) for record in records))

    def test_policy_approval_and_unknown_still_stop_before_later_write(self):
        for kind in ("path", "approval", "unknown"):
            with self.subTest(kind=kind):
                registry = ToolRegistry(ToolContext(WorkspacePolicy(self.workspace), CommandPolicy(), lambda *_: kind != "approval"))
                patch_text = VALID
                if kind == "path":
                    patch_text = "--- /dev/null\n+++ b/.git/config\n@@ -0,0 +1,1 @@\n+blocked\n"
                provider = StrictQueueProvider([response("apply_patch", {"patch": patch_text}),
                    response("edit_file", {"path": "app.py", "old_text": "1", "new_text": "9"})])
                handler = registry._handlers["apply_patch"]
                original_run = handler.run
                def execute(arguments):
                    if kind == "unknown":
                        handler._mark_journal_tainted("app.py")
                        return tool_failure(ErrorCode.RESULT_UNCERTAIN, "fixed")
                    return original_run(arguments)
                with patch.object(handler, "run", side_effect=execute):
                    result = CodingAgent(provider, registry, max_rounds=2, plan_enabled=False).run("update")
                self.assertFalse(result.ok)
                self.assertEqual(1, len(provider.histories))
                self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text(encoding="utf8"))


if __name__ == "__main__":
    unittest.main()
