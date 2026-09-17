"""错误分类必须来自执行边界事实，不能从自由文本或扩展字段取得信任。"""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder import execution_state as state
from tricoder.agent import CodingAgent
from tricoder.audit import AuditLogger
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.core.events import ToolExecutionCompleted
from tricoder.extensions.models import ToolOrigin
from tricoder.mcp.models import MCPCallResult, MCPToolSpec
from tricoder.mcp.tool_adapter import MCPToolHandler, normalize_mcp_result
from tricoder.models import ProviderResponse, SessionContext, ToolAction, ToolCall, ToolResult
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy
from tricoder.protocols import LegacyJsonProtocol, NativeToolProtocol
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.handlers import ToolHandler

FAKE = "TRICODER_FAKE_SECRET_9f31"


class ExternalTool(ToolHandler):
    name = "external"
    description = "本地测试替身"
    parameters = ToolHandler._schema({"payload": {"type": "string"}})

    def run(self, arguments):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class ToolErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = ToolContext(WorkspacePolicy(self.root), CommandPolicy(self.root),
                                   lambda *_: False)
        self.registry = ToolRegistry(self.context)

    def check_error(self, result, code, recovery):
        self.assertFalse(result.ok)
        error = getattr(result, "error", None)
        self.assertIsNotNone(error, "真实工具出口缺少机器可读错误")
        self.assertEqual(code, error.code.value)
        self.assertEqual(recovery, error.recovery.value)
        self.assertIs(False, error.retryable)

    def execute_both(self, name, arguments):
        return (self.registry.execute(name, arguments),
                asyncio.run(self.registry.execute_async(name, arguments)))

    def test_registry_preflight_table(self):
        cases = [
            ("not_registered", {}, "unknown_tool", "replan"),
            ("read_file", {"path": 7}, "invalid_argument", "replan"),
            ("read_file", {"path": ""}, "invalid_argument", "replan"),
            ("read_file", {"path": "../outside"}, "policy_denied", "stop_task"),
            ("run_command", {"command": "git reset --hard"}, "policy_denied", "stop_task"),
            ("run_command", {"command": "python -m unittest"}, "approval_denied", "stop_task"),
            ("create_file", {"path": "a.txt", "content": FAKE}, "approval_denied", "stop_task"),
            ("read_file", {"path": "."}, "invalid_argument", "replan"),
            ("search_text", {"query": "[", "use_regex": True}, "invalid_argument", "replan"),
            ("glob_files", {"pattern": "../*"}, "invalid_argument", "replan"),
        ]
        for name, args, code, recovery in cases:
            with self.subTest(name=name, code=code):
                for result in self.execute_both(name, args):
                    self.check_error(result, code, recovery)
                    self.assertNotIn(FAKE, result.output)

    def test_missing_paths_replan_while_security_denials_stop(self):
        missing_cases = (
            ("read_file", {"path": "missing.py"}),
            ("list_files", {"path": "missing-dir"}),
            ("search_text", {"path": "missing-dir", "query": "needle"}),
            ("glob_files", {"path": "missing-dir", "pattern": "*.py"}),
        )
        for name, arguments in missing_cases:
            with self.subTest(kind="missing", name=name):
                for result in self.execute_both(name, arguments):
                    self.check_error(result, "invalid_argument", "replan")

        denied_cases = (
            ("read_file", {"path": "../outside.txt"}),
            ("read_file", {"path": ".env.local"}),
        )
        for name, arguments in denied_cases:
            with self.subTest(kind="denied", path=arguments["path"]):
                for result in self.execute_both(name, arguments):
                    self.check_error(result, "policy_denied", "stop_task")

    def test_real_command_nonzero_timeout_and_output_limit(self):
        self.context.approver = lambda *_: True
        cases = [("raise SystemExit(7)", 5, 2000, "execution_failed", "stop_task"),
                 ("import time; time.sleep(2)", .05, 2000, "timeout", "stop_task"),
                 ("print('x' * 20000)", 5, 100, "output_limit", "stop_task")]
        for source, timeout, limit, code, recovery in cases:
            with self.subTest(code=code):
                (self.root / "probe.py").write_text(source, encoding="utf-8")
                self.context.timeout, self.context.max_output_chars = timeout, limit
                for result in self.execute_both("run_command", {"command": "python probe.py"}):
                    self.check_error(result, code, recovery)

    def external(self, value, origin_kind="mcp"):
        handler = ExternalTool(self.context)
        handler.value = value
        self.registry.register(handler, origin=ToolOrigin(origin_kind, "fixture", "read"))
        return handler

    def test_external_structures_and_exception_text_are_untrusted(self):
        handler = self.external(None)
        for value, code in [(None, "invalid_result"), (ToolResult(False, FAKE), "invalid_result"),
                            (ValueError(FAKE), "result_uncertain"),
                            (PolicyError(FAKE), "result_uncertain"),
                            (RuntimeError(FAKE), "result_uncertain")]:
            with self.subTest(code=code, value_type=type(value).__name__):
                handler.value = value
                for result in self.execute_both("external", {"payload": FAKE}):
                    self.check_error(result, code, "stop_task")
                    self.assertNotIn(FAKE, result.output)

    def test_local_error_contract_and_external_forgery(self):
        self.assertTrue(hasattr(state, "ToolError"), "缺少 ToolError 公开契约")
        error = state.ToolError(state.ErrorCode.INVALID_ARGUMENT, state.RecoveryAction.REPLAN)
        handler = self.external(ToolResult(False, FAKE, error=error))
        for result in self.execute_both("external", {}):
            self.check_error(result, "invalid_result", "stop_task")
        handler.value = ToolResult(True, FAKE, error=error)
        for result in self.execute_both("external", {}):
            self.check_error(result, "invalid_result", "stop_task")
        builtin = self.registry._handlers["list_files"]
        with patch.object(builtin, "run", return_value=ToolResult(True, "ok", error=error)):
            with self.assertRaises(TypeError):
                self.registry.execute("list_files", {})

    def test_unknown_builtin_exceptions_and_cancellation_keep_identity(self):
        handler = self.registry._handlers["list_files"]
        for error in (RuntimeError(FAKE), TypeError(FAKE), ValueError(FAKE),
                      CancellationError("cancel"), KeyboardInterrupt(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), patch.object(handler, "run", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    self.registry.execute("list_files", {})
                self.assertIs(error, caught.exception)

    def test_search_unknown_value_error_is_not_swallowed_after_validation(self):
        """真实扫描已进入文件读取，未知编程异常不能被解释成没有匹配。"""
        (self.root / "broken.txt").write_text("needle", encoding="utf-8")
        primary = ValueError(FAKE)
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), patch.object(Path, "read_bytes", side_effect=primary):
                with self.assertRaises(ValueError) as caught:
                    if asynchronous:
                        asyncio.run(self.registry.execute_async("search_text", {"query": "needle"}))
                    else:
                        self.registry.execute("search_text", {"query": "needle"})
                self.assertIs(primary, caught.exception)

    def test_search_expected_read_failure_keeps_other_matches(self):
        """单个不可读文件和非 UTF-8 文件仍按既有扫描契约跳过，不污染其他匹配。"""
        (self.root / "broken.txt").write_text("needle", encoding="utf-8")
        (self.root / "invalid.txt").write_bytes(b"\xff")
        (self.root / "good.txt").write_text("needle", encoding="utf-8")
        real_read = Path.read_bytes

        def read(path):
            if path.name == "broken.txt":
                raise OSError(FAKE)
            return real_read(path)

        with patch.object(Path, "read_bytes", read):
            for result in self.execute_both("search_text", {"query": "needle"}):
                self.assertTrue(result.ok)
                self.assertIsNone(result.error)
                self.assertIn("good.txt:1", result.output)
                self.assertNotIn("broken.txt", result.output)
                self.assertNotIn("invalid.txt", result.output)
                self.assertNotIn(FAKE, result.output)

    def test_success_and_legacy_position_arguments(self):
        result = self.registry.execute("list_files", {})
        self.assertTrue(result.ok)
        self.assertIsNone(getattr(result, "error", "missing"))
        old = ToolResult(True, "ok", "a.txt", ("a.txt",), (), 2, True, None, 0, None,
                         state.FileEffects(state.EffectState.CONFIRMED, ("a.txt",)))
        self.assertIsNone(old.error)
        self.assertEqual(("a.txt",), old.file_effects.paths)

    def test_mcp_preflight_and_post_request_uncertainty(self):
        class Manager:
            async def call_tool(inner, *args):
                raise RuntimeError(FAKE)
        spec = MCPToolSpec("fixture", "read", "mcp__fixture__read", "fixture",
                           ToolHandler._schema({}))
        handler = MCPToolHandler(self.context, Manager(), spec)
        self.registry.register(handler, origin=ToolOrigin("mcp", "fixture", "dangerous"))
        denied = asyncio.run(self.registry.execute_async(handler.name, {}))
        self.check_error(denied, "approval_denied", "stop_task")
        self.context.approver = lambda *_: True
        failed = asyncio.run(self.registry.execute_async(handler.name, {}))
        self.check_error(failed, "result_uncertain", "stop_task")
        self.assertNotIn(FAKE, failed.output)

    def test_mcp_invalid_wire_result_is_not_success(self):
        for value in ({}, {"content": FAKE}, {"content": [], "isError": FAKE},
                      {"content": [{"type": "text"}]}):
            with self.subTest(value=value):
                self.assertFalse(normalize_mcp_result(value).ok)

    def test_mcp_invalid_content_discriminators_are_rejected_at_registry_boundary(self):
        """远端块必须先有合法 type，未知但明确的未来类型才能安全省略。"""
        class Manager:
            async def call_tool(inner, *args):
                return normalize_mcp_result({"content": [inner.block], "isError": False})
        manager = Manager()
        spec = MCPToolSpec("fixture", "read", "mcp__fixture__read", "fixture", ToolHandler._schema({}))
        handler = MCPToolHandler(self.context, manager, spec)
        self.registry.register(handler, origin=ToolOrigin("mcp", "fixture", "read"))
        for block in ({}, None, {"type": None}, {"type": 7}, {"type": []},
                      {"type": ""}, {"type": "\n"}):
            manager.block = block
            with self.subTest(block=block):
                result = asyncio.run(self.registry.execute_async(handler.name, {}))
                self.check_error(result, "invalid_result", "stop_task")
        manager.block = {"type": "future_block", "payload": FAKE}
        result = asyncio.run(self.registry.execute_async(handler.name, {}))
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertNotIn(FAKE, result.output)

    def test_protocols_and_ui_show_only_stable_error(self):
        result = self.registry.execute("read_file", {"path": "../" + FAKE})
        action = ToolAction("read_file", {"path": FAKE}, "controlled")
        payloads = [json.loads(protocol.tool_result_message(action, result, "c1").content)
                    for protocol in (NativeToolProtocol(), LegacyJsonProtocol())]
        for payload in payloads:
            self.assertEqual({"code": "policy_denied", "recovery": "stop_task", "retryable": False},
                             payload["tool_result"].get("error"))
            self.assertNotIn(FAKE, json.dumps(payload))
        from rich.console import Console
        from tricoder.ui import TerminalUI
        from tricoder.tui import TuiObserver
        stream = io.StringIO()
        ui = TerminalUI(console=Console(file=stream, force_terminal=False))
        ui.on_tool_result(action, result, 1)
        class App:
            def round_line(self, value): stream.write(str(value))
            def round_summary(self, value): stream.write(str(value))
        TuiObserver(App()).on_tool_result(action, result, 1)
        self.assertIn("policy_denied", stream.getvalue())
        self.assertIn("stop_task", stream.getvalue())
        self.assertNotIn(FAKE, stream.getvalue())

    def test_legacy_parse_failure_has_stable_error_without_source_text(self):
        resolved = LegacyJsonProtocol().resolve_action(ProviderResponse(content=FAKE))
        payload = json.loads(resolved.feedback.content)["tool_result"]
        self.assertEqual({"code": "invalid_argument", "recovery": "replan", "retryable": False},
                         payload.get("error"))
        self.assertNotIn(FAKE, resolved.feedback.content)

    def test_agent_cancel_boundary_and_audit_are_structured(self):
        class Provider:
            def complete(inner, messages, tools=()):
                return ProviderResponse(tool_calls=(ToolCall("c1", "list_files", {}),))
        handler = self.registry._handlers["list_files"]
        events = []
        with patch.object(handler, "run", side_effect=CancellationError(FAKE)):
            agent = CodingAgent(Provider(), self.registry, plan_enabled=False)
            turn = asyncio.run(agent.run_with_context_async("fixture", SessionContext(),
                                                           event_sink=events.append))
        completed = [event for event in events if isinstance(event, ToolExecutionCompleted)]
        self.assertEqual(1, len(completed))
        self.check_error(completed[0].result, "cancelled", "stop_task")
        self.assertFalse(turn.file_effects_observed)
        self.assertNotIn(FAKE, str(turn))

    def test_agent_failure_audit_omits_unvalidated_arguments(self):
        class Provider:
            def complete(inner, messages, tools=()):
                return ProviderResponse(tool_calls=(ToolCall("c1", "read_file", {"path": "../" + FAKE}),))
        audit_path = self.root / "audit.jsonl"
        agent = CodingAgent(Provider(), self.registry, audit=AuditLogger(audit_path),
                            max_rounds=1, plan_enabled=False)
        agent.run("fixture")
        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertNotIn(FAKE, audit_text)
        records = [json.loads(line) for line in audit_text.splitlines()]
        failed = next(record for record in records if record.get("status") == "tool_error")
        self.assertEqual("policy_denied", failed.get("error", {}).get("code"))

    def test_command_failure_after_process_started_is_never_replan(self):
        from tricoder import subprocess_control as control
        self.context.approver = lambda *_: True
        (self.root / "probe.py").write_text("import time; time.sleep(2)", encoding="utf-8")
        real_terminate = control._terminate_process_tree
        for cleanup_ok, code in ((True, "result_uncertain"), (False, "cleanup_failed")):
            def terminate(process, env, job, *, deadline=None):
                real_terminate(process, env, job, deadline=deadline)
                return cleanup_ok
            with self.subTest(code=code), patch.object(control, "_collect_bounded_process", side_effect=OSError(FAKE)), \
                    patch.object(control, "_terminate_process_tree", side_effect=terminate):
                result = self.registry.execute("run_command", {"command": "python probe.py"})
                self.check_error(result, code, "stop_task")
                self.assertNotIn(FAKE, result.output)

    def test_command_start_failure_and_cleanup_evidence_are_distinct(self):
        from tricoder import subprocess_control as control
        self.context.approver = lambda *_: True
        with patch.object(control.subprocess, "Popen", side_effect=OSError(FAKE)):
            result = self.registry.execute("run_command", {"command": "python -m unittest"})
        self.check_error(result, "execution_failed", "replan")
        from tricoder.tools import command
        with patch.object(command, "run_bounded_process", return_value=control.BoundedProcessResult(
                1, FAKE, "", cleanup_failed=True)):
            result = self.registry.execute("run_command", {"command": "python -m unittest"})
        self.check_error(result, "cleanup_failed", "stop_task")
        self.assertNotIn(FAKE, result.output)

    def test_invalid_path_format_is_pre_execution_argument_error(self):
        for result in self.execute_both("read_file", {"path": "bad\x00path"}):
            self.check_error(result, "invalid_argument", "replan")

    def test_command_parse_failure_is_argument_error_not_policy_decision(self):
        for command in ('python "unterminated', "   "):
            with self.subTest(command=command):
                for result in self.execute_both("run_command", {"command": command}):
                    self.check_error(result, "invalid_argument", "replan")

    def test_read_only_policy_is_classified_before_approval(self):
        self.context.read_only = True
        for name, args in (("create_file", {"path": "a.txt", "content": FAKE}),
                           ("run_command", {"command": "python -m unittest"})):
            for result in self.execute_both(name, args):
                self.check_error(result, "policy_denied", "stop_task")

    def test_forged_builtin_origin_cannot_bypass_error_trust_or_approval(self):
        handler = self.external(ToolResult(False, FAKE), origin_kind="builtin")
        for result in self.execute_both(handler.name, {}):
            self.check_error(result, "invalid_result", "stop_task")
            self.assertNotIn(FAKE, result.output)

    def test_unknown_effects_override_replan_and_keep_confirmed_paths(self):
        from tricoder.changes import ChangeJournal
        from tricoder.tools.binding import _DirectoryBinding
        from tests.test_tools import ExternalReplacementBinding
        for name in ("aaa.py", "app.py", "other.py"):
            (self.root / name).write_text("value = 1\n", encoding="utf-8")
        journal = ChangeJournal()
        journal.begin_task((), "通过")
        self.context.change_journal = journal
        self.context.approver = lambda *_: True
        source = "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
                         for name in ("aaa.py", "app.py", "other.py"))
        flags = {}
        class Binding(ExternalReplacementBinding):
            def replace(self, temporary_name, target_name):
                if target_name == "aaa.py" and flags.get("external_replaced"):
                    raise OSError("fictional rollback failure")
                return super().replace(temporary_name, target_name)
        real_open = _DirectoryBinding.open
        with patch.object(_DirectoryBinding, "open", side_effect=lambda root, parent: Binding(real_open(root, parent), flags)):
            result = self.registry.execute("apply_patch", {"patch": source})
        self.check_error(result, "execution_failed", "stop_task")
        self.assertEqual(state.EffectState.UNKNOWN, result.file_effects.state)
        self.assertEqual(("aaa.py", "app.py"), result.file_effects.paths)
        self.assertEqual(("aaa.py", "app.py"), journal.active_effects().paths)

    def test_patch_race_classification_preserves_compensation_and_effect_facts(self):
        """同为 PolicyError 的发布前拒绝和发布后不确定，必须由产生点保留不同类别。"""
        from tricoder.changes import ChangeJournal
        from tricoder.tools.binding import _DirectoryBinding
        from tests.test_tools import LateCommitRaceBinding
        real_open = _DirectoryBinding.open
        self.context.approver = lambda *_: True
        source = "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-before\n+after\n"
                         for name in ("a.txt", "b.txt"))
        cases = (("before", False, "policy_denied"), ("after", False, "result_uncertain"),
                 ("after", True, "result_uncertain"))
        for stage, residual, code in cases:
            for name in ("a.txt", "b.txt"):
                (self.root / name).write_text("before\n", encoding="utf-8")
            journal = ChangeJournal()
            journal.begin_task((), "通过")
            self.context.change_journal = journal

            class Binding(LateCommitRaceBinding):
                def replace(inner, temporary, target):
                    if residual and inner.replaced and target == "a.txt":
                        raise OSError("fictional rollback failure")
                    return super().replace(temporary, target)

            with self.subTest(stage=stage, residual=residual), patch.object(
                _DirectoryBinding, "open", side_effect=lambda root, parent: Binding(
                    real_open(root, parent), stage=stage, target="b.txt", content="external\n"),
            ):
                result = self.registry.execute("apply_patch", {"patch": source})
                self.check_error(result, code, "stop_task")
                self.assertEqual("external\n", (self.root / "b.txt").read_text(encoding="utf-8"))
                self.assertEqual("after\n" if residual else "before\n",
                                 (self.root / "a.txt").read_text(encoding="utf-8"))
                self.assertEqual(state.EffectState.UNKNOWN, result.file_effects.state)
                self.assertEqual(("a.txt", "b.txt") if residual else ("b.txt",), result.file_effects.paths)
                self.assertEqual(result.file_effects, journal.active_effects())
                sealed = journal.seal_task(result.file_effects.paths, "待验证")
                self.assertIn("b.txt", sealed.tainted_paths)
                self.assertNotIn("external\n", repr(sealed))

    def test_mcp_local_failure_categories_and_remote_error_forgery(self):
        from tricoder.mcp.client import MCPCleanupError, MCPTimeoutError
        from tricoder.mcp.manager import MCPPreflightError
        class Manager:
            async def call_tool(inner, *args):
                if isinstance(inner.value, Exception):
                    raise inner.value
                return inner.value
        manager = Manager()
        spec = MCPToolSpec("fixture", "read", "mcp__fixture__read", "fixture", ToolHandler._schema({}))
        handler = MCPToolHandler(self.context, manager, spec)
        self.registry.register(handler, origin=ToolOrigin("mcp", "fixture", "read"))
        cases = [(MCPPreflightError(FAKE), "policy_denied"),
                 (MCPTimeoutError(FAKE), "timeout"), (MCPCleanupError(FAKE), "cleanup_failed"),
                 (MCPCallResult(False, FAKE), "invalid_result"),
                 (ToolResult(False, FAKE, error=state.ToolError(state.ErrorCode.INVALID_ARGUMENT,
                                                              state.RecoveryAction.REPLAN)), "invalid_result")]
        for value, code in cases:
            manager.value = value
            with self.subTest(code=code):
                result = asyncio.run(self.registry.execute_async(handler.name, {}))
                self.check_error(result, code, "stop_task")
                self.assertNotIn(FAKE, result.output)

    def test_observable_command_diagnostics_remain_available_to_protocols(self):
        self.context.approver = lambda *_: True
        (self.root / "probe.py").write_text("print('fixture diagnostic'); raise SystemExit(7)", encoding="utf-8")
        result = self.registry.execute("run_command", {"command": "python probe.py"})
        self.check_error(result, "execution_failed", "stop_task")
        for protocol in (NativeToolProtocol(), LegacyJsonProtocol()):
            message = protocol.tool_result_message(ToolAction("run_command", {}, "fixture"), result, "c1")
            self.assertIn("fixture diagnostic", message.content)
            self.assertIn("execution_failed", message.content)

    def test_external_success_cannot_forge_audit_or_verification_metadata(self):
        self.external(ToolResult(True, "safe", audit_paths=(FAKE,), verification_passed=True,
                                 spill_reference=FAKE, spill_bytes=9, spill_sha256=FAKE))
        for result in self.execute_both("external", {}):
            self.assertTrue(result.ok)
            self.assertEqual((), result.audit_paths)
            self.assertIsNone(result.spill_reference)
            self.assertIsNone(result.verification_passed)
            self.assertNotIn(FAKE, repr(result))

    def test_unknown_post_publish_snapshot_exception_preserves_primary_and_taint(self):
        from tricoder.changes import ChangeJournal
        real_snapshot = ToolHandler._snapshot
        self.context.approver = lambda *_: True
        for tool in ("create_file", "edit_file"):
            for error_type in (RuntimeError, TypeError, ValueError):
                name = f"{tool}_{error_type.__name__}.txt"
                target = self.root / name
                if tool == "edit_file":
                    target.write_text("before\n", encoding="utf-8")
                journal = ChangeJournal()
                journal.begin_task((), "通过")
                self.context.change_journal = journal
                primary = error_type(FAKE)
                def snapshot(binding, leaf, relative):
                    actual = real_snapshot(binding, leaf, relative)
                    if leaf == name and actual.content == "after\n":
                        raise primary
                    return actual
                args = ({"path": name, "content": "after\n"} if tool == "create_file" else
                        {"path": name, "old_text": "before", "new_text": "after"})
                with self.subTest(tool=tool, error_type=error_type.__name__), \
                        patch.object(ToolHandler, "_snapshot", side_effect=snapshot):
                    with self.assertRaises(error_type) as caught:
                        self.registry.execute(tool, args)
                    self.assertIs(primary, caught.exception)
                    self.assertEqual("after\n", target.read_text(encoding="utf-8"))
                    self.assertEqual(state.EffectState.UNKNOWN, journal.active_effects().state)

    def test_unknown_patch_publish_exception_is_rethrown_after_compensation(self):
        from tricoder.changes import ChangeJournal
        from tricoder.tools.binding import _DirectoryBinding
        self.context.approver = lambda *_: True
        real_open = _DirectoryBinding.open
        for error_type in (RuntimeError, TypeError, ValueError):
            for name in ("a.txt", "b.txt"):
                (self.root / name).write_text("before\n", encoding="utf-8")
            journal = ChangeJournal()
            journal.begin_task((), "通过")
            self.context.change_journal = journal
            primary = error_type(FAKE)
            class Binding:
                def __init__(inner, binding): inner.binding = binding
                def __getattr__(inner, key): return getattr(inner.binding, key)
                def replace(inner, temporary, target):
                    if target == "b.txt": raise primary
                    return inner.binding.replace(temporary, target)
            source = "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-before\n+after\n"
                             for name in ("a.txt", "b.txt"))
            with self.subTest(error_type=error_type.__name__), \
                    patch.object(_DirectoryBinding, "open", side_effect=lambda root, parent: Binding(real_open(root, parent))):
                with self.assertRaises(error_type) as caught:
                    self.registry.execute("apply_patch", {"patch": source})
                self.assertIs(primary, caught.exception)
                self.assertEqual("before\n", (self.root / "a.txt").read_text(encoding="utf-8"))
                self.assertEqual(state.EffectState.NONE, journal.active_effects().state)

    def test_unknown_post_commit_journal_exception_is_not_reported_as_success(self):
        from tricoder.changes import ChangeJournal
        self.context.approver = lambda *_: True
        journal = ChangeJournal()
        journal.begin_task((), "通过")
        self.context.change_journal = journal
        primary = RuntimeError(FAKE)
        with patch.object(ToolHandler, "_record_committed", side_effect=primary):
            with self.assertRaises(RuntimeError) as caught:
                self.registry.execute("create_file", {"path": "actual.txt", "content": "after\n"})
        self.assertIs(primary, caught.exception)
        self.assertEqual("after\n", (self.root / "actual.txt").read_text(encoding="utf-8"))
        self.assertEqual(state.EffectState.UNKNOWN, journal.active_effects().state)


if __name__ == "__main__":
    unittest.main()
