"""工作区基线三分支门禁与精确复扫确认。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tricoder.models import (
    AppConfig,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionTurnResult,
)
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore
from tricoder.workspace.gate import WorkspaceGatePreview
from tricoder.workspace.snapshot import WorkspaceScanError, capture_workspace_baseline


class _Agent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_with_context(self, task, context, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(task)
        return SessionTurnResult(RunResult(True, "synthetic", 1), context)


def _builder(agent: _Agent):
    def build(record, memory, _options):  # type: ignore[no-untyped-def]
        return ActiveSession(
            record,
            memory,
            SessionContext(persisted_summary=memory.summary),
            AppConfig(
                workspace=record.workspace,
                provider=ProviderConfig("openai", "synthetic", "https://example.invalid", "model"),
            ),
            agent,
        )

    return build


class WorkspaceGateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "code.py").write_text("value = 1\n", encoding="utf-8")
        self.store = SessionStore(self.root / "state" / "sessions.db")
        self.store.initialize(self.workspace)

    def runtime(self, agent: _Agent, **kwargs) -> SessionRuntime:  # type: ignore[no-untyped-def]
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(agent),
            **kwargs,
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_new_session_initial_scan_needs_no_confirmation_and_unchanged_next_task_runs(self) -> None:
        """若新 Session 初扫也强制确认，会破坏无变化直接执行规则。"""

        agent = _Agent()
        previews: list[WorkspaceGatePreview] = []
        runtime = self.runtime(agent, workspace_confirmer=lambda preview: previews.append(preview) or True)

        self.assertTrue(runtime.run_task("first").ok)
        self.assertTrue(runtime.run_task("second").ok)

        self.assertEqual(["first", "second"], agent.calls)
        self.assertEqual([], previews)

    def test_changed_workspace_rejection_does_not_call_agent_or_append_task_message(self) -> None:
        """若先写消息或调用 Provider 再确认，拒绝已无法做到零副作用。"""

        agent = _Agent()
        previews: list[WorkspaceGatePreview] = []
        runtime = self.runtime(agent, workspace_confirmer=lambda preview: previews.append(preview) or False)
        runtime.run_task("baseline")
        active = runtime._require_active_session()
        before_context = active.context
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        with self.assertRaisesRegex(SessionRuntimeError, "已拒绝工作区变化"):
            runtime.run_task("must-not-run")

        self.assertEqual(["baseline"], agent.calls)
        after_context = runtime._require_active_session().context
        self.assertEqual(before_context.messages, after_context.messages)
        self.assertEqual("待验证", after_context.verification)
        self.assertEqual(1, len(previews))
        self.assertEqual("changed", previews[0].kind)
        self.assertIn("code.py", previews[0].changed_paths)

    def test_accepted_change_is_rescanned_and_a_second_change_requires_new_confirmation(self) -> None:
        """若确认后不复扫，确认窗口中的编辑会借用旧批准执行。"""

        agent = _Agent()
        previews: list[WorkspaceGatePreview] = []

        def confirm(preview: WorkspaceGatePreview) -> bool:
            previews.append(preview)
            if len(previews) == 1:
                (self.workspace / "code.py").write_text("value = 3\n", encoding="utf-8")
            return True

        runtime = self.runtime(agent, workspace_confirmer=confirm)
        runtime.run_task("baseline")
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        result = runtime.run_task("after-confirm")

        # 任务确实执行一次，外部变化使旧验证失效并建立历史义务；这个
        # synthetic Agent 没有产生本轮修改，因此回顾可交付但义务不能丢失。
        self.assertTrue(result.ok)
        self.assertEqual("pending", runtime.current.memory.verification_obligation)
        self.assertIn("code.py", runtime.current.memory.pending_verification_paths)
        self.assertEqual(["baseline", "after-confirm"], agent.calls)
        self.assertEqual(2, len(previews))
        self.assertNotEqual(previews[0].candidate_id, previews[1].candidate_id)
        self.assertNotEqual(previews[0].preview_id, previews[1].preview_id)

    def test_changed_workspace_without_confirmer_is_blocked_even_in_fullaccess(self) -> None:
        """若门禁复用工具自动审批，fullaccess 会静默接受外部代码变化。"""

        agent = _Agent()
        runtime = self.runtime(agent)
        runtime.run_task("baseline")
        runtime.set_permission("fullaccess")
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        with self.assertRaisesRegex(SessionRuntimeError, "无法确认工作区变化"):
            runtime.run_task("must-not-run")
        self.assertEqual(["baseline"], agent.calls)

    def test_scan_failure_blocks_before_session_creation_and_agent_call(self) -> None:
        """若扫描失败走初始化分支，首次任务仍会留下 Session 或调用 Provider。"""

        agent = _Agent()

        def fail_scan(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise WorkspaceScanError("limit_exceeded")

        runtime = self.runtime(agent, workspace_snapshotter=fail_scan)
        with self.assertRaisesRegex(SessionRuntimeError, "limit_exceeded"):
            runtime.run_task("must-not-run")
        self.assertEqual([], agent.calls)
        self.assertEqual([], self.store.list_all())

    def test_restored_session_without_memory_baseline_requires_explicit_initialization(self) -> None:
        """若重启后静默建基线，重启期间的外部修改永远不会展示。"""

        record = self.store.create("restored", self.workspace, "openai", "model")
        denied_agent = _Agent()
        denied_previews: list[WorkspaceGatePreview] = []
        denied = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(denied_agent),
            initial_session_id=record.id,
            workspace_confirmer=lambda preview: denied_previews.append(preview) or False,
        )
        self.addCleanup(denied.close)

        with self.assertRaisesRegex(SessionRuntimeError, "初始化"):
            denied.run_task("denied")
        self.assertEqual([], denied_agent.calls)
        self.assertEqual("initialize", denied_previews[0].kind)
        denied.close()

        accepted_agent = _Agent()
        accepted_previews: list[WorkspaceGatePreview] = []
        accepted = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(accepted_agent),
            initial_session_id=record.id,
            workspace_confirmer=lambda preview: accepted_previews.append(preview) or True,
        )
        self.addCleanup(accepted.close)
        self.assertTrue(accepted.run_task("accepted").ok)
        self.assertEqual(["accepted"], accepted_agent.calls)
        self.assertEqual("initialize", accepted_previews[0].kind)

    def test_confirmation_cancellation_invalidates_approval(self) -> None:
        """若确认回调期间的取消未复核，迟到的 True 会启动任务。"""

        agent = _Agent()
        runtime: SessionRuntime

        def confirm(_preview: WorkspaceGatePreview) -> bool:
            self.assertTrue(runtime.cancel_current())
            return True

        runtime = self.runtime(agent, workspace_confirmer=confirm)
        runtime.run_task("baseline")
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        result = runtime.run_task("must-not-run")
        self.assertFalse(result.ok)
        self.assertEqual("任务已取消", result.summary)
        self.assertEqual(["baseline"], agent.calls)

    def test_root_identity_change_is_safely_blocked_before_agent(self) -> None:
        """相同路径/清单不能掩盖工作区根对象替换，也不能泄漏 ValueError。"""

        calls = 0

        def changed_root(root, limits, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            baseline = capture_workspace_baseline(root, limits, **kwargs)
            if calls >= 3:
                return replace(
                    baseline,
                    root_identity=tuple(value + 1 for value in baseline.root_identity),
                )
            return baseline

        agent = _Agent()
        runtime = self.runtime(agent, workspace_snapshotter=changed_root)
        runtime.run_task("baseline")

        with self.assertRaisesRegex(SessionRuntimeError, "快照身份或范围"):
            runtime.run_task("must-not-run")

        self.assertEqual(["baseline"], agent.calls)
