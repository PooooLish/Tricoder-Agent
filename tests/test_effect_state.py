import tempfile
import unittest
import asyncio
import os
import sqlite3
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.changes import ChangeJournal
from tricoder.models import ProviderResponse, SessionContext, ToolCall
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry
from tests.test_agent import StructuredScriptedProvider
from tests.test_tools import patch_binding_publish_failure, ExternalReplacementBinding
from tricoder.tools.binding import _DirectoryBinding
from tricoder.tools.handlers import ToolHandler
from tricoder.extensions import ToolOrigin
from tricoder.models import ToolResult
from tests.test_session_runtime import RegistryJournalFactory
from tricoder.models import SessionMemory
from tricoder.models import RunResult, SessionTurnResult
from tricoder.core.cancellation import CancellationError
from tricoder.session_runtime import RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.sessions import SessionStore, SessionError
from tricoder.shell import InteractiveShell
from tricoder.tui import TricoderApp
from tests.test_shell import FakeUI


class RealResidualTests(unittest.TestCase):
    def test_real_patch_residual_reaches_agent_and_invalidates_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "src").mkdir()
            first = workspace / "src/app.py"
            second = workspace / "src/other.py"
            first.write_text("value = 1\n", encoding="utf-8")
            second.write_text("value = 1\n", encoding="utf-8")
            journal = ChangeJournal()
            journal.begin_task((), "通过")
            registry = ToolRegistry(ToolContext(
                WorkspacePolicy(workspace), CommandPolicy(), lambda *_: True,
                change_journal=journal,
            ))
            patch_text = "".join(
                f"--- a/src/{name}\n+++ b/src/{name}\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
                for name in ("app.py", "other.py")
            )
            provider = StructuredScriptedProvider([
                ProviderResponse(tool_calls=(ToolCall("patch", "apply_patch", {"patch": patch_text}),)),
                ProviderResponse(tool_calls=(ToolCall("finish", "finish", {"summary": "done"}),)),
            ])
            agent = CodingAgent(provider, registry, max_rounds=2, plan_enabled=False)
            with patch_binding_publish_failure(rollback_fails=True):
                turn = agent.run_with_context("apply patch", SessionContext(verification="通过"))
            self.assertEqual("value = 2\n", first.read_text(encoding="utf-8"))
            self.assertEqual("value = 1\n", second.read_text(encoding="utf-8"))
            self.assertEqual(("src/app.py",), turn.result.modified_files)
            self.assertEqual("待验证", turn.result.verification)
            self.assertFalse(turn.result.ok)
            self.assertEqual(("src/app.py",), turn.context.modified_files)

    def test_compensation_uses_operation_before_not_earlier_task_write(self):
        from tricoder.execution_state import EffectState
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = workspace / "app.py"
            first.write_text("value = 0\n", encoding="utf-8")
            (workspace / "other.py").write_text("value = 1\n", encoding="utf-8")
            journal = ChangeJournal()
            journal.begin_task((), "未运行")
            registry = ToolRegistry(ToolContext(WorkspacePolicy(workspace), CommandPolicy(),
                                               lambda *_: True, change_journal=journal))
            for old, new in (("0", "1"), ("1", "2")):
                self.assertTrue(registry.execute("edit_file", {"path": "app.py", "old_text": old, "new_text": new}).ok)
            patch_text = ("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 2\n+value = 3\n"
                          "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n")
            with patch_binding_publish_failure():
                result = registry.execute("apply_patch", {"patch": patch_text})
            self.assertFalse(result.ok)
            self.assertEqual(EffectState.NONE, result.file_effects.state)
            self.assertEqual("value = 2\n", first.read_text(encoding="utf-8"))
            with patch_binding_publish_failure(rollback_fails=True):
                residual = registry.execute("apply_patch", {"patch": patch_text})
            self.assertEqual(("app.py",), residual.file_effects.paths)
            self.assertEqual(("app.py",), journal.active_effects().paths)
            change = journal.seal_task(("app.py",), "待验证").changes[0]
            self.assertEqual("value = 0\n", change.before.content)
            self.assertEqual("value = 3\n", change.after.content)

    def test_partial_confirmed_residual_and_identity_change_stop_agent(self):
        from tricoder.execution_state import EffectState
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for name in ("aaa.py", "app.py", "other.py"):
                (workspace / name).write_text("value = 1\n", encoding="utf-8")
            journal = ChangeJournal()
            journal.begin_task((), "通过")
            registry = ToolRegistry(ToolContext(WorkspacePolicy(workspace), CommandPolicy(),
                                               lambda *_: True, change_journal=journal))
            patch_text = "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
                                 for name in ("aaa.py", "app.py", "other.py"))
            real_open = _DirectoryBinding.open
            state = {}

            class PartialBinding(ExternalReplacementBinding):
                def replace(self, temporary_name, target_name):
                    if target_name == "aaa.py" and state.get("external_replaced"):
                        raise OSError("fictional rollback failure")
                    return super().replace(temporary_name, target_name)

            provider = StructuredScriptedProvider([
                ProviderResponse(tool_calls=(
                    ToolCall("patch", "apply_patch", {"patch": patch_text}),
                    ToolCall("finish", "finish", {"summary": "done"}),
                )),
            ])
            agent = CodingAgent(provider, registry, max_rounds=2, plan_enabled=False)
            with patch.object(_DirectoryBinding, "open", side_effect=lambda root, parent: PartialBinding(real_open(root, parent), state)):
                turn = agent.run_with_context("partial patch", SessionContext(verification="通过"))
            self.assertFalse(turn.result.ok)
            self.assertTrue(turn.result.unknown_effects)
            self.assertTrue(turn.context.unknown_effects)
            self.assertEqual(1, turn.result.tool_calls)
            self.assertEqual(("aaa.py", "app.py"), turn.result.modified_files)
            self.assertEqual("value = 2\n", (workspace / "aaa.py").read_text(encoding="utf-8"))
            self.assertEqual("external = True\n", (workspace / "app.py").read_text(encoding="utf-8"))
            effects = journal.active_effects()
            self.assertEqual(EffectState.UNKNOWN, effects.state)
            self.assertEqual(("aaa.py", "app.py"), effects.paths)
            self.assertNotIn("external = True", repr(journal.seal_task(effects.paths, "待验证")))

    def test_extension_and_forged_builtin_cannot_mint_effects(self):
        from tricoder.execution_state import EffectState, FileEffects
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(ToolContext(WorkspacePolicy(Path(directory)), CommandPolicy(), lambda *_: True))

            class ClaimingTool(ToolHandler):
                name = "claim"
                description = "fictional extension"
                parameters = ToolHandler._schema({})

                def run(self, arguments):
                    return ToolResult(False, "rejected", relative_path="../outside", modified_paths=("fake.py",),
                                      file_effects=FileEffects(EffectState.CONFIRMED, ("fake.py",)))

            for kind in ("mcp", "builtin"):
                with self.subTest(kind=kind):
                    tool = ClaimingTool(registry.context)
                    tool.name = "claim_" + kind
                    registry.register(tool, origin=ToolOrigin(kind, "fiction", "write"))
                    result = registry.execute(tool.name, {})
                    self.assertEqual(FileEffects(EffectState.UNKNOWN), result.file_effects)
                    self.assertIsNone(result.relative_path)
                    self.assertEqual((), result.modified_paths)


class EffectStateTests(unittest.TestCase):
    def test_existing_positional_constructors_keep_their_meaning(self):
        tool = ToolResult(True, "ok", "a.py", ("a.py",), (), 0, True, "ref", 10, "hash")
        self.assertIsNone(tool.file_effects)
        self.assertEqual("hash", tool.spill_sha256)
        result = RunResult(True, "ok", 1, 2, ("a.py",), "通过", None)
        self.assertFalse(result.unknown_effects)
        context = SessionContext((), "summary", ("a.py",), "通过")
        self.assertFalse(context.unknown_effects)
        memory = SessionMemory("s", "r", "t", ("a.py",), "通过", "strict")
        self.assertFalse(memory.unknown_effects)
        self.assertEqual("strict", memory.permission_level)

    def test_residual_write_invalidates_previous_verification(self):
        from tricoder.execution_state import EffectState, ExecutionState, FileEffects
        updated = ExecutionState(verification="通过").observe(
            FileEffects(EffectState.CONFIRMED, ("a.py",)))
        self.assertEqual(("a.py",), updated.modified_files)
        self.assertEqual("待验证", updated.verification)

    def test_uncertainty_survives_a_later_noop(self):
        from tricoder.execution_state import EffectState, ExecutionState, FileEffects
        state = ExecutionState().observe(FileEffects(EffectState.UNKNOWN))
        self.assertTrue(state.observe(FileEffects(EffectState.NONE)).unknown_effects)

    def test_invalid_effect_contract_is_rejected(self):
        from tricoder.execution_state import EffectState, FileEffects
        for state, paths in [("none", ()), (EffectState.NONE, ("a.py",)),
                             (EffectState.CONFIRMED, ()), (EffectState.UNKNOWN, (1,)),
                             (EffectState.UNKNOWN, ("../outside",)),
                             (EffectState.UNKNOWN, ("C:/outside",)),
                             (EffectState.UNKNOWN, ("a/../b",))]:
            with self.subTest(state=state, paths=paths), self.assertRaises(ValueError):
                FileEffects(state, paths)

    def test_taint_without_net_changes_survives_sealing(self):
        from tricoder.execution_state import EffectState
        journal = ChangeJournal()
        journal.begin_task((), "通过")
        journal.mark_tainted("a.py")
        self.assertEqual(EffectState.UNKNOWN, journal.active_effects().state)
        sealed = journal.seal_task((), "待验证")
        self.assertIsNotNone(sealed)
        self.assertEqual(("a.py",), sealed.tainted_paths)


class RuntimeEffectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = SessionStore(self.root / "state.db")
        self.runtime = self.restart()

    def restart(self):
        return SessionRuntime(self.store, self.workspace, options=RuntimeOptions(environ={}),
                              active_session_factory=RegistryJournalFactory())

    def test_real_patch_residual_is_consistent_across_runtime_disk_memory_and_journal(self):
        for name in ("app.py", "other.py"):
            (self.workspace / name).write_text("value = 1\n", encoding="utf-8")
        patch_text = "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
                             for name in ("app.py", "other.py"))
        provider = StructuredScriptedProvider([
            ProviderResponse(tool_calls=(ToolCall("patch", "apply_patch", {"patch": patch_text}),)),
            ProviderResponse(tool_calls=(ToolCall("finish", "finish", {"summary": "done"}),)),
        ])
        agent = CodingAgent(provider, self.runtime.current.tools, max_rounds=2, plan_enabled=False)
        self.runtime.current = replace(self.runtime.current, agent=agent,
                                       context=SessionContext(verification="通过"))
        with patch_binding_publish_failure(rollback_fails=True):
            result = self.runtime.run_task("fictional patch")
        self.assertEqual("value = 2\n", (self.workspace / "app.py").read_text(encoding="utf-8"))
        self.assertEqual("value = 1\n", (self.workspace / "other.py").read_text(encoding="utf-8"))
        self.assertFalse(result.ok)
        self.assertEqual(("app.py",), result.modified_files)
        self.assertEqual("待验证", result.verification)
        self.assertEqual(result.modified_files, self.runtime.current.context.modified_files)
        memory = self.store.load_memory(self.runtime.current.record.id)
        self.assertEqual(result.modified_files, memory.modified_files)
        self.assertEqual(result.verification, memory.verification)
        sealed = self.runtime.current.journal.latest()
        self.assertEqual(result.modified_files, sealed.after_modified_files)
        self.assertEqual("value = 2\n", sealed.changes[0].after.content)

    def test_unknown_builtin_exception_after_real_publish_preserves_main_error(self):
        registry = self.runtime.current.tools
        handler = registry._handlers["create_file"]
        real_run = handler.run
        primary = RuntimeError("BUILTIN-PRIMARY")

        def fail_after_publish(arguments):
            real_run(arguments)
            raise primary

        provider = StructuredScriptedProvider([
            ProviderResponse(tool_calls=(ToolCall("create", "create_file", {"path": "actual.py", "content": "fiction\n"}),)),
        ])
        agent = CodingAgent(provider, registry, max_rounds=1, plan_enabled=False)
        self.runtime.current = replace(self.runtime.current, agent=agent)
        with patch.object(handler, "run", side_effect=fail_after_publish):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.run_task("fictional write")
        self.assertIs(primary, caught.exception)
        self.assertEqual("fiction\n", (self.workspace / "actual.py").read_text(encoding="utf-8"))
        self.assertEqual(("actual.py",), self.runtime.current.context.modified_files)
        self.assertEqual("待验证", self.runtime.current.context.verification)

    def test_same_path_write_then_cancellation_invalidates_old_verification_before_next_finish(self):
        for previous_verification in ("通过", "passed"):
            with self.subTest(previous_verification=previous_verification):
                runtime = self.restart()
                target = self.workspace / "same.py"
                target.write_text("value = 1\n", encoding="utf-8")
                registry = runtime.current.tools
                handler = registry._handlers["edit_file"]
                real_run = handler.run

                def cancel_after_publish(arguments):
                    real_run(arguments)
                    raise CancellationError("fictional cancellation after completed write")

                provider = StructuredScriptedProvider([
                    ProviderResponse(tool_calls=(ToolCall("edit", "edit_file", {
                        "path": "same.py", "old_text": "1", "new_text": "2",
                    }),)),
                    ProviderResponse(tool_calls=(ToolCall("finish", "finish", {"summary": "done"}),)),
                ])
                agent = CodingAgent(provider, registry, max_rounds=1, plan_enabled=False)
                runtime.current = replace(runtime.current, agent=agent,
                    context=SessionContext(modified_files=("same.py",), verification=previous_verification))
                with patch.object(handler, "run", side_effect=cancel_after_publish):
                    cancelled = runtime.run_task("fictional edit")
                self.assertEqual("value = 2\n", target.read_text(encoding="utf-8"))
                self.assertFalse(cancelled.ok)
                self.assertEqual(("same.py",), cancelled.modified_files)
                self.assertEqual("待验证", cancelled.verification)
                self.assertEqual("待验证", runtime.current.context.verification)
                self.assertEqual("待验证", self.store.load_memory(runtime.current.record.id).verification)
                self.assertEqual("待验证", runtime.current.journal.latest().after_verification)
                finished = runtime.run_task("finish without new verification")
                self.assertFalse(finished.ok)
                self.assertEqual("待验证", finished.verification)

    def test_consumed_same_path_write_and_real_verification_can_finish_successfully(self):
        target = self.workspace / "same.py"
        target.write_text("value = 1\n", encoding="utf-8")
        provider = StructuredScriptedProvider([
            ProviderResponse(tool_calls=(ToolCall("edit", "edit_file", {
                "path": "same.py", "old_text": "1", "new_text": "2",
            }),)),
            ProviderResponse(tool_calls=(ToolCall("verify", "run_command", {
                "command": "python -m compileall -q same.py",
            }),)),
            ProviderResponse(tool_calls=(ToolCall("finish", "finish", {"summary": "done"}),)),
        ])
        agent = CodingAgent(provider, self.runtime.current.tools, max_rounds=3, plan_enabled=False)
        self.runtime.current = replace(self.runtime.current, agent=agent,
            context=SessionContext(modified_files=("same.py",), verification="通过"))
        result = self.runtime.run_task("fictional edit and verification")
        self.assertEqual("value = 2\n", target.read_text(encoding="utf-8"))
        self.assertTrue(result.ok)
        self.assertEqual("通过", result.verification)
        self.assertEqual("passed", self.store.load_memory(self.runtime.current.record.id).verification)
        self.assertEqual("通过", self.runtime.current.journal.latest().after_verification)

    def test_real_write_then_cancel_or_exception_preserves_state_and_primary(self):
        for primary in (asyncio.CancelledError(), RuntimeError("PRIMARY")):
            with self.subTest(primary=type(primary).__name__):
                runtime = self.restart()
                registry = runtime.current.tools
                name = type(primary).__name__ + ".py"

                class ExplodingAgent:
                    def run_with_context(self, task, context):
                        result = registry.execute("create_file", {"path": name, "content": "fiction\n"})
                        if not result.ok:
                            raise AssertionError(result.output)
                        raise primary

                runtime.current = replace(runtime.current, agent=ExplodingAgent())
                with self.assertRaises(type(primary)) as caught:
                    runtime.run_task("write then interrupt")
                self.assertIs(primary, caught.exception)
                self.assertEqual("fiction\n", (self.workspace / name).read_text(encoding="utf-8"))
                self.assertIn(name, runtime.current.context.modified_files)
                self.assertIn(name, self.store.load_memory(runtime.current.record.id).modified_files)
                self.assertEqual("待验证", runtime.current.context.verification)
                self.assertEqual(name, runtime.current.journal.latest().changes[0].path)

    def test_zero_net_change_taint_reconciles_even_if_agent_omits_result(self):
        target = self.workspace / "a.py"
        target.write_text("value = 1\n", encoding="utf-8")
        registry = self.runtime.current.tools

        class OmittingAgent:
            def run_with_context(self, task, context):
                registry.execute("edit_file", {"path": "a.py", "old_text": "1", "new_text": "2"})
                registry.execute("edit_file", {"path": "a.py", "old_text": "2", "new_text": "1"})
                replacement = target.with_name("external.py")
                replacement.write_text("EXTERNAL-SOURCE-NEVER-PERSIST\n", encoding="utf-8")
                os.replace(replacement, target)
                registry.execute("edit_file", {"path": "a.py", "old_text": "EXTERNAL", "new_text": "replaced"})
                return SessionTurnResult(RunResult(True, "done", 1), context)

        self.runtime.current = replace(self.runtime.current, agent=OmittingAgent())
        result = self.runtime.run_task("fictional task")
        self.assertFalse(result.ok)
        self.assertTrue(result.unknown_effects)
        self.assertEqual(("a.py",), result.modified_files)
        self.assertEqual("待验证", result.verification)
        sealed = self.runtime.current.journal.latest()
        self.assertEqual((), sealed.changes)
        self.assertEqual(("a.py",), sealed.tainted_paths)
        with self.assertRaises(SessionRuntimeError):
            self.runtime.prepare_undo()
        self.assertTrue(self.restart().current.memory.unknown_effects)
        self.assertNotIn(b"EXTERNAL-SOURCE-NEVER-PERSIST", self.store.database_path.read_bytes())

    def test_confirmed_residual_missing_from_agent_cannot_return_success(self):
        registry = self.runtime.current.tools

        class OmittingAgent:
            def run_with_context(self, task, context):
                registry.execute("create_file", {"path": "residual.py", "content": "fiction\n"})
                return SessionTurnResult(RunResult(True, "done", 1), context)

        self.runtime.current = replace(self.runtime.current, agent=OmittingAgent())
        result = self.runtime.run_task("fictional task")
        self.assertEqual(("residual.py",), result.modified_files)
        self.assertEqual("待验证", result.verification)
        self.assertFalse(result.ok)

    def test_unknown_blocks_undo_of_previously_clean_journal(self):
        registry = self.runtime.current.tools
        journal = self.runtime.current.journal
        journal.begin_task((), "未运行")
        registry.execute("create_file", {"path": "keep.py", "content": "fiction\n"})
        journal.seal_task(("keep.py",), "待验证")
        self.runtime.current = replace(self.runtime.current,
                                       memory=SessionMemory(unknown_effects=True))
        with self.assertRaises(SessionRuntimeError):
            self.runtime.undo_latest()
        self.assertTrue((self.workspace / "keep.py").exists())

    def test_seal_and_persistence_failures_never_replace_primary_exception(self):
        registry = self.runtime.current.tools
        journal = self.runtime.current.journal
        primary = RuntimeError("PRIMARY")

        class ExplodingAgent:
            def run_with_context(self, task, context):
                registry.execute("create_file", {"path": "actual.py", "content": "fiction\n"})
                raise primary

        self.runtime.current = replace(self.runtime.current, agent=ExplodingAgent())
        real_seal = journal.seal_task

        def failing_seal(*args):
            real_seal(*args)
            raise ValueError("secondary seal failure")

        with patch.object(journal, "seal_task", side_effect=failing_seal), patch.object(
            self.runtime, "_persist_current", side_effect=OSError("secondary persistence failure")
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.run_task("fictional task")
        self.assertIs(primary, caught.exception)
        self.assertEqual(("actual.py",), self.runtime.current.context.modified_files)
        self.assertEqual(("actual.py",), journal.latest().after_modified_files)

    def test_normal_seal_failure_keeps_completed_write_in_memory(self):
        registry = self.runtime.current.tools
        journal = self.runtime.current.journal

        class WritingAgent:
            def run_with_context(self, task, context):
                registry.execute("create_file", {"path": "actual.py", "content": "fiction\n"})
                return SessionTurnResult(RunResult(False, "incomplete", 1), context)

        self.runtime.current = replace(self.runtime.current, agent=WritingAgent())
        primary = RuntimeError("SEAL-PRIMARY")
        real_seal = journal.seal_task

        def failing_seal(*args):
            real_seal(*args)
            raise primary

        with patch.object(journal, "seal_task", side_effect=failing_seal):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.run_task("fictional task")
        self.assertIs(primary, caught.exception)
        self.assertEqual(("actual.py",), self.runtime.current.context.modified_files)
        self.assertEqual(("actual.py",), self.store.load_memory(self.runtime.current.record.id).modified_files)

    def test_returning_original_context_cannot_reuse_old_verification_for_same_path(self):
        target = self.workspace / "same.py"
        target.write_text("value = 1\n", encoding="utf-8")
        registry = self.runtime.current.tools

        class OmittingAgent:
            def run_with_context(self, task, context):
                registry.execute("edit_file", {"path": "same.py", "old_text": "1", "new_text": "2"})
                return SessionTurnResult(RunResult(True, "done", 1), context)

        self.runtime.current = replace(self.runtime.current, agent=OmittingAgent(),
                                       context=SessionContext(modified_files=("same.py",), verification="通过"))
        result = self.runtime.run_task("fictional task")
        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)

    def test_unknown_restart_block_and_explicit_clear_does_not_restore_files(self):
        path = self.workspace / "retained.py"
        path.write_text("fiction\n", encoding="utf-8")
        session_id = self.runtime.current.record.id
        self.store.save_memory(session_id, SessionMemory(unknown_effects=True, verification="待验证"))
        runtime = self.restart()
        self.assertTrue(runtime.current.context.unknown_effects)
        self.assertFalse(runtime.run_task("write something").ok)
        with self.assertRaises(SessionRuntimeError):
            runtime.clear_current()
        self.assertTrue(self.store.load_memory(session_id).unknown_effects)
        runtime.clear_current(confirmed=True)
        self.assertFalse(self.restart().current.context.unknown_effects)
        self.assertEqual("fiction\n", path.read_text(encoding="utf-8"))

    def test_unknown_roundtrip_and_invalid_storage_values_rejected(self):
        session_id = self.runtime.current.record.id
        self.store.save_memory(session_id, SessionMemory(unknown_effects=True))
        self.assertTrue(self.store.load_memory(session_id).unknown_effects)
        for invalid in (2, -1, "yes", None):
            with self.subTest(value=invalid), self.assertRaises(SessionError):
                self.store.save_memory(session_id, SessionMemory(unknown_effects=invalid))
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            connection.execute("UPDATE session_memory SET unknown_effects=2")
            connection.commit()
        with self.assertRaises(SessionError):
            self.store.load_memory(session_id)

    def test_shell_and_tui_clear_confirm_only_records_not_files(self):
        for frontend in ("shell", "tui"):
            with self.subTest(frontend=frontend):
                session_id = self.runtime.current.record.id
                self.store.save_memory(session_id, SessionMemory(unknown_effects=True))
                runtime = self.restart()
                prompts = []
                answers = iter((False, True))

                def confirm(prompt):
                    prompts.append(prompt)
                    return next(answers)

                if frontend == "shell":
                    ui = FakeUI([])
                    ui.confirm = confirm
                    shell = InteractiveShell(runtime, ui)
                    clear = lambda: shell.execute("/clear")
                else:
                    app = TricoderApp(lambda *_: runtime)
                    app.runtime = runtime
                    app._confirm = confirm
                    app.log_line_safe = lambda *_: None
                    clear = app._clear_confirm_worker
                clear()
                self.assertTrue(runtime.current.memory.unknown_effects)
                clear()
                self.assertFalse(runtime.current.memory.unknown_effects)
                self.assertTrue(all("不恢复文件" in prompt for prompt in prompts))

    def test_shell_and_tui_status_keep_unknown_notice(self):
        self.store.save_memory(self.runtime.current.record.id, SessionMemory(unknown_effects=True))
        runtime = self.restart()
        ui = FakeUI([])
        InteractiveShell(runtime, ui).execute("/status")
        self.assertTrue(any("未确认" in text for text in ui.text))
        app = TricoderApp(lambda *_: runtime)
        app.runtime = runtime
        lines = []
        app.log_line = lambda text: lines.append(str(text))
        app._show_status()
        self.assertTrue(any("未确认" in text for text in lines))

    def test_old_database_migration_idempotence_and_transaction_rollback(self):
        database = self.root / "old.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE session_memory (session_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
                    requirements_summary TEXT NOT NULL DEFAULT '', last_task_summary TEXT NOT NULL DEFAULT '',
                    modified_files_json TEXT NOT NULL DEFAULT '[]', verification TEXT NOT NULL DEFAULT '未运行');
                INSERT INTO session_memory(session_id) VALUES ('old');
            """)
        store = SessionStore(database)
        with patch.object(store, "_ensure_permission_column", side_effect=sqlite3.OperationalError("migration failure")):
            with self.assertRaises(SessionError):
                store.initialize(self.workspace)
        with closing(sqlite3.connect(database)) as connection:
            self.assertNotIn("unknown_effects", [row[1] for row in connection.execute("PRAGMA table_info(session_memory)")])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM session_memory").fetchone()[0])
        store.initialize(self.workspace)
        store.initialize(self.workspace)
        with closing(sqlite3.connect(database)) as connection:
            self.assertIn("unknown_effects", [row[1] for row in connection.execute("PRAGMA table_info(session_memory)")])
        self.assertFalse(store.load_memory("old").unknown_effects)
