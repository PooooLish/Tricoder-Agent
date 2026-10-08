"""持久化工作区基线的激活、重启、拒绝与安全门禁回归。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import MethodType
from pathlib import Path

from tricoder.changes import FileChange, FileIdentity, FileSnapshot, TaskChangeSet
from tricoder.models import (
    AppConfig,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionMemory,
    SessionTurnResult,
)
from tricoder.session.runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.session.store import SessionStore
from tricoder.workspace.gate import WorkspaceGatePreview
from tricoder.workspace.lock import WorkspaceLock


class _Agent:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.contexts: list[SessionContext] = []

    def run_with_context(self, task, context, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(task)
        self.contexts.append(context)
        return SessionTurnResult(RunResult(True, "synthetic", 1), context)


def _builder(agent: _Agent):
    def build(record, memory, _options):  # type: ignore[no-untyped-def]
        return ActiveSession(
            record,
            memory,
            SessionContext(
                persisted_summary=memory.summary,
                verification=memory.verification,
                verification_obligation=memory.verification_obligation,
                pending_verification_paths=memory.pending_verification_paths,
            ),
            AppConfig(
                workspace=record.workspace,
                provider=ProviderConfig(
                    "openai", "synthetic", "https://example.invalid", "model"
                ),
            ),
            agent,
        )

    return build


class WorkspaceBaselineRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "code.py").write_text("value = 1\n", encoding="utf-8")
        self.store = SessionStore(self.root / "state" / "sessions.db")
        self.store.initialize(self.workspace)

    def _runtime(
        self,
        agent: _Agent,
        *,
        session_id: str | None = None,
        confirmer=None,  # type: ignore[no-untyped-def]
    ) -> SessionRuntime:
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(agent),
            initial_session_id=session_id,
            workspace_confirmer=confirmer,
        )
        self.addCleanup(runtime.close)
        return runtime

    def _seed(self, *, memory: SessionMemory | None = None):  # type: ignore[no-untyped-def]
        record = self.store.create("seed", self.workspace, "openai", "model")
        if memory is not None:
            self.store.save_memory(record.id, memory)
        bootstrap = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(_Agent()),
            initial_session_id=record.id,
            workspace_confirmer=lambda _preview: self.fail(
                "首次完整扫描不应请求确认"
            ),
        )
        self.assertTrue(bootstrap.close())
        self.assertIsNotNone(self.store.load_workspace_baseline(record.id))
        return record

    def _install_owned_write(self, runtime: SessionRuntime) -> None:
        target = self.workspace / "code.py"

        def controlled(_runtime: SessionRuntime, _task: str) -> RunResult:
            before_stat = target.stat()
            before = FileSnapshot(
                "code.py",
                target.read_text("utf-8"),
                before_stat.st_mode & 0o7777,
                FileIdentity(before_stat.st_dev, before_stat.st_ino),
            )
            with target.open("w", encoding="utf-8", newline="") as stream:
                stream.write("value = 2\n")
            after_stat = target.stat()
            after = FileSnapshot(
                "code.py",
                "value = 2\n",
                after_stat.st_mode & 0o7777,
                FileIdentity(after_stat.st_dev, after_stat.st_ino),
            )
            _runtime._task_sealed_change_set = TaskChangeSet(
                (FileChange("code.py", before, after),),
                (),
                "待验证",
                ("code.py",),
                "通过",
            )
            return RunResult(
                True,
                "synthetic",
                1,
                modified_files=("code.py",),
                verification="通过",
            )

        runtime._run_task_locked = MethodType(controlled, runtime)  # type: ignore[method-assign]

    def test_restart_with_equal_baseline_needs_no_confirmation(self) -> None:
        """稳定内容一致时重启应直接安装新鲜运行期快照。"""

        record = self._seed()
        previews: list[WorkspaceGatePreview] = []
        agent = _Agent()
        runtime = self._runtime(
            agent,
            session_id=record.id,
            confirmer=lambda preview: previews.append(preview) or False,
        )

        self.assertTrue(runtime.run_task("review").ok)
        self.assertEqual(["review"], agent.calls)
        self.assertEqual([], previews)

    def test_activation_rejection_keeps_target_session_and_old_record(self) -> None:
        """拒绝变化只取消工作区操作，目标 Session 与旧 revision 仍保留。"""

        record = self._seed()
        before = self.store.load_workspace_baseline(record.id)
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")
        previews: list[WorkspaceGatePreview] = []
        agent = _Agent()
        runtime = self._runtime(
            agent,
            session_id=record.id,
            confirmer=lambda preview: previews.append(preview) or False,
        )

        self.assertEqual(record.id, runtime.current.record.id)
        self.assertEqual(before, self.store.load_workspace_baseline(record.id))
        with self.assertRaisesRegex(SessionRuntimeError, "已拒绝工作区变化"):
            runtime.run_task("must-not-run")
        self.assertEqual([], agent.calls)
        self.assertGreaterEqual(len(previews), 1)

    def test_activation_acceptance_persists_and_first_task_does_not_prompt_again(self) -> None:
        """激活接受必须先持久化；同一候选不能在首任务重复询问。"""

        record = self._seed()
        before = self.store.load_workspace_baseline(record.id)
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")
        previews: list[WorkspaceGatePreview] = []
        agent = _Agent()
        runtime = self._runtime(
            agent,
            session_id=record.id,
            confirmer=lambda preview: previews.append(preview) or True,
        )

        after_activation = self.store.load_workspace_baseline(record.id)
        self.assertEqual(before.revision + 1, after_activation.revision)
        self.assertEqual(1, len(previews))
        self.assertEqual("accepted", runtime.status().workspace_baseline_state)
        self.assertTrue(runtime.run_task("continue").ok)
        self.assertEqual(1, len(previews))
        self.assertEqual(["continue"], agent.calls)

        self.assertTrue(runtime.close())
        restarted_previews: list[WorkspaceGatePreview] = []
        restarted = self._runtime(
            _Agent(),
            session_id=record.id,
            confirmer=lambda preview: restarted_previews.append(preview) or False,
        )
        self.assertEqual([], restarted_previews)
        self.assertEqual(record.id, restarted.current.record.id)

    def test_same_content_atomic_replace_does_not_prompt(self) -> None:
        """内容相同不弹确认；历史展示可恢复，可信 evidence 不可恢复。"""

        record = self._seed()
        replacement = self.workspace / "replacement.py"
        replacement.write_text("value = 1\n", encoding="utf-8")
        replacement.replace(self.workspace / "code.py")
        previews: list[WorkspaceGatePreview] = []
        agent = _Agent()

        runtime = self._runtime(
            agent,
            session_id=record.id,
            confirmer=lambda preview: previews.append(preview) or False,
        )

        self.assertEqual([], previews)
        self.assertTrue(runtime.run_task("review").ok)
        self.assertIsNone(agent.contexts[-1].verification_evidence)

    def test_corrupted_record_blocks_provider_and_is_not_reinitialized(self) -> None:
        """损坏记录必须留存并阻断，不能折叠为首次使用。"""

        record = self._seed()
        with closing(sqlite3.connect(self.store.database_path)) as connection, connection:
            connection.execute(
                "UPDATE session_workspace_baselines SET payload_json = '{' "
                "WHERE session_id = ?",
                (record.id,),
            )
        agent = _Agent()
        runtime = self._runtime(agent, session_id=record.id, confirmer=lambda _preview: True)

        with self.assertRaisesRegex(SessionRuntimeError, "历史基线不可用"):
            runtime.run_task("must-not-run")
        self.assertEqual([], agent.calls)
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            payload = connection.execute(
                "SELECT payload_json FROM session_workspace_baselines WHERE session_id = ?",
                (record.id,),
            ).fetchone()[0]
        self.assertEqual("{", payload)

    def test_accepting_change_preserves_pending_obligation_and_adds_changed_path(self) -> None:
        """认可起点不是验证通过，旧 pending 与新增变化路径都必须保留。"""

        memory = SessionMemory(
            verification="failed",
            verification_obligation="pending",
            pending_verification_paths=("old.py",),
        )
        record = self._seed(memory=memory)
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        runtime = self._runtime(
            _Agent(), session_id=record.id, confirmer=lambda _preview: True
        )

        self.assertEqual("pending", runtime.current.memory.verification_obligation)
        self.assertEqual(
            {"old.py", "code.py"},
            set(runtime.current.memory.pending_verification_paths),
        )
        self.assertNotEqual("passed", runtime.current.memory.verification)

    def test_attributable_task_change_advances_persisted_baseline(self) -> None:
        """合法任务收尾必须同步推进 SQLite，否则重启会重复询问自身改动。"""

        record = self._seed()
        before = self.store.load_workspace_baseline(record.id)
        runtime = self._runtime(_Agent(), session_id=record.id, confirmer=lambda _p: True)
        self._install_owned_write(runtime)

        runtime.run_task("owned-write")

        after = self.store.load_workspace_baseline(record.id)
        self.assertEqual(before.revision + 1, after.revision)
        self.assertTrue(runtime.close())
        previews: list[WorkspaceGatePreview] = []
        restarted = self._runtime(
            _Agent(),
            session_id=record.id,
            confirmer=lambda preview: previews.append(preview) or False,
        )
        self.assertEqual([], previews)
        self.assertEqual(record.id, restarted.current.record.id)

    def test_finalize_storage_failure_keeps_old_record_and_fails_task(self) -> None:
        """收尾 CAS 失败不能只推进内存基线并把任务宣称为已跨重启认可。"""

        record = self._seed()
        before = self.store.load_workspace_baseline(record.id)
        previews: list[WorkspaceGatePreview] = []
        runtime = self._runtime(
            _Agent(),
            session_id=record.id,
            confirmer=lambda preview: previews.append(preview) or False,
        )
        self._install_owned_write(runtime)
        original_save = self.store.save_workspace_baseline

        def fail_save(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError("synthetic storage failure")

        self.store.save_workspace_baseline = fail_save  # type: ignore[method-assign]
        try:
            result = runtime.run_task("owned-write")
        finally:
            self.store.save_workspace_baseline = original_save  # type: ignore[method-assign]

        self.assertFalse(result.ok)
        self.assertIn("基线持久化失败", result.summary)
        self.assertEqual(before, self.store.load_workspace_baseline(record.id))
        with self.assertRaisesRegex(SessionRuntimeError, "已拒绝工作区变化"):
            runtime.run_task("next")
        self.assertEqual(("code.py",), previews[-1].changed_paths)

    def test_two_sessions_keep_independent_baselines_across_a_b_a_switch(self) -> None:
        """B 接受当前状态不能覆盖 A；切回 A 拒绝后仍选中 A 且旧记录不变。"""

        first = self._seed()
        second = self.store.create("second", self.workspace, "openai", "model")
        bootstrap = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(_Agent()),
            initial_session_id=second.id,
            workspace_confirmer=lambda _preview: True,
        )
        self.assertTrue(bootstrap.close())
        first_before = self.store.load_workspace_baseline(first.id)
        previews: list[WorkspaceGatePreview] = []
        decisions = iter((True, False))

        def confirm(preview: WorkspaceGatePreview) -> bool:
            previews.append(preview)
            return next(decisions)

        runtime = self._runtime(_Agent(), session_id=first.id, confirmer=confirm)
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")
        runtime.switch(second.id, confirm=lambda _workspace: True)
        second_after = self.store.load_workspace_baseline(second.id)
        runtime.switch(first.id, confirm=lambda _workspace: True)

        self.assertEqual(first.id, runtime.current.record.id)
        self.assertEqual(first_before, self.store.load_workspace_baseline(first.id))
        self.assertGreater(second_after.revision, 1)
        self.assertEqual(2, len(previews))

    def test_activation_lock_busy_keeps_session_pending_and_task_retries(self) -> None:
        """激活锁忙不能取消 Session；锁释放后任务仍须重新扫描并继续。"""

        record = self._seed()
        ownership = WorkspaceLock.acquire(self.workspace)
        self.addCleanup(ownership.close)
        agent = _Agent()
        runtime = self._runtime(
            agent,
            session_id=record.id,
            confirmer=lambda _preview: self.fail("一致基线不应确认"),
        )

        self.assertEqual("pending", runtime.status().workspace_baseline_state)
        self.assertEqual(record.id, runtime.current.record.id)
        ownership.close()
        self.assertTrue(runtime.run_task("retry").ok)
        self.assertEqual(["retry"], agent.calls)

    def test_new_explicit_session_initializes_without_inheriting_old_session(self) -> None:
        """新 Session 以当前完整扫描初建独立记录，不继承旧 Session revision。"""

        first = self._seed()
        runtime = self._runtime(_Agent(), session_id=first.id, confirmer=lambda _p: True)
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        created = runtime.create("new")

        first_stored = self.store.load_workspace_baseline(first.id)
        created_stored = self.store.load_workspace_baseline(created.record.id)
        self.assertEqual(1, first_stored.revision)
        self.assertEqual(1, created_stored.revision)
        self.assertNotEqual(
            first_stored.record.content_digest,
            created_stored.record.content_digest,
        )


if __name__ == "__main__":
    unittest.main()
