"""Session 独占与交接：真实文件锁、SQLite 和独立 Python 进程。"""

import os
import io
import asyncio
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tricoder.models import (
    AppConfig,
    Message,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionTurnResult,
)
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore


class _SyntheticAgent:
    def run_with_context(self, task, context, **_kwargs):
        return SessionTurnResult(RunResult(True, "synthetic", 1), context)


def build_session(record, memory, options):
    config = AppConfig(
        workspace=record.workspace,
        provider=ProviderConfig(record.provider, "synthetic", "https://example.invalid", record.model),
    )
    return ActiveSession(
        record,
        memory,
        SessionContext(persisted_summary=memory.summary),
        config,
        _SyntheticAgent(),
    )


class _SlowSyntheticAgent(_SyntheticAgent):
    def run_with_context(self, task, context, **kwargs):
        time.sleep(0.5)
        return super().run_with_context(task, context, **kwargs)


def build_slow_session(record, memory, options):
    return replace(build_session(record, memory, options), agent=_SlowSyntheticAgent())


class SessionOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "one"
        self.other = self.root / "two"
        self.workspace.mkdir()
        self.other.mkdir()
        self.store = SessionStore(self.root / "state" / "sessions.db")
        self.store.initialize(self.workspace)
        self.first = self.store.create("first", self.workspace, "openai", "one")
        self.second = self.store.create("second", self.other, "openai", "two")

    def runtime(self, workspace=None, **kwargs):
        selected_workspace = (workspace or self.workspace).resolve()
        if "initial_session_id" not in kwargs:
            latest = self.store.latest_for_workspace(selected_workspace)
            if latest is not None:
                kwargs["initial_session_id"] = latest.id
        runtime = SessionRuntime(
            self.store, selected_workspace, options=RuntimeOptions(environ={}),
            active_session_factory=kwargs.pop("active_session_factory", build_session),
            workspace_confirmer=kwargs.pop("workspace_confirmer", lambda _preview: True),
            **kwargs,
        )
        self.addCleanup(lambda: getattr(runtime, "close", lambda: None)())
        return runtime

    def assert_busy(self, workspace):
        with self.assertRaisesRegex(SessionRuntimeError, "占用"):
            self.runtime(workspace)

    def test_second_runtime_is_rejected_before_building_or_loading_memory(self):
        self.runtime()
        def forbidden(*args):
            self.fail("被占用的会话不应装配工具或清理临时结果")
        with self.assertRaisesRegex(SessionRuntimeError, "占用"):
            self.runtime(active_session_factory=forbidden)

    def test_different_sessions_can_be_opened_and_busy_switch_preserves_current(self):
        first = self.runtime()
        second = self.runtime(self.other)
        original = first.current
        with self.assertRaisesRegex(SessionRuntimeError, "占用"):
            first.switch(self.second.id, confirm=lambda _: True)
        self.assertIs(original, first.current)
        self.assertEqual(self.second.id, second.current.record.id)
        self.assert_busy(self.workspace)

    def test_successful_switch_releases_old_session_and_reloads_latest_metadata(self):
        runtime = self.runtime()
        runtime.current = replace(runtime.current, context=SessionContext(messages=(Message("user", "private-history"),)))
        old_context = runtime.current.context
        runtime.switch(self.second.id, confirm=lambda _: True)
        peer = self.runtime()
        peer.rename_current("changed-by-peer")
        peer.close()
        runtime.switch(self.first.id, confirm=lambda _: True)
        self.assertEqual("changed-by-peer", runtime.current.record.name)
        self.assertEqual((), runtime.current.context.messages)
        self.assertIsNot(old_context, runtime.current.context)
        self.assertIsNone(runtime.diff_latest())
        self.runtime(self.other)

    def test_new_session_releases_old_session(self):
        runtime = self.runtime()
        runtime.create("new")
        # 旧会话所在工作区的 latest 已变化，另一终端从其他工作区进入后切回旧 ID。
        peer = self.runtime(self.other)
        peer.switch(self.first.id, confirm=lambda _: True)
        self.assertEqual(self.first.id, peer.current.record.id)

    def test_failed_switch_releases_candidate_and_keeps_old_session(self):
        def builder(record, memory, options):
            if record.id == self.second.id:
                raise ValueError("synthetic build failure")
            return build_session(record, memory, options)
        runtime = self.runtime(active_session_factory=builder)
        original = runtime.current
        with self.assertRaises(SessionRuntimeError):
            runtime.switch(self.second.id, confirm=lambda _: True)
        self.assertIs(original, runtime.current)
        self.assert_busy(self.workspace)
        self.runtime(self.other)

    def test_failed_initialization_releases_lock_even_on_interrupt(self):
        def interrupted(*args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.runtime(active_session_factory=interrupted)
        self.runtime()

    def test_failed_persistence_keeps_old_session_and_releases_target(self):
        runtime = self.runtime()
        original = runtime.current
        runtime.current = replace(original, memory=replace(original.memory, summary="pending"))
        runtime._memory_dirty = True
        with mock.patch.object(self.store, "save_memory", side_effect=OSError("synthetic write failure")):
            with self.assertRaisesRegex(SessionRuntimeError, "未持久化"):
                runtime.switch(self.second.id, confirm=lambda _: True)
        self.assertEqual(self.first.id, runtime.current.record.id)
        self.assertEqual("pending", runtime.current.memory.summary)
        self.assert_busy(self.workspace)
        self.runtime(self.other)

    def test_failed_pending_clear_prevents_session_handoff(self):
        runtime = self.runtime()
        runtime.current = replace(runtime.current, context=replace(runtime.current.context, memory_pending_clear=True))
        with mock.patch.object(self.store, "clear_conversation_memory", side_effect=OSError("synthetic clear failure")):
            with self.assertRaises(SessionRuntimeError):
                runtime.switch(self.second.id, confirm=lambda _: True)
        self.assertTrue(runtime.current.context.memory_pending_clear)
        self.assert_busy(self.workspace)
        self.runtime(self.other)

    def test_unreaped_resources_prevent_switch_and_close_from_releasing_lock(self):
        from tricoder.task_cleanup import TaskCleanup
        class Resource:
            reaped = False
            def cleanup(self, deadline):
                return self.reaped
        runtime = self.runtime()
        resource = Resource()
        scope = TaskCleanup()
        scope.retain(resource)
        runtime._pending_cleanup.append(scope)
        try:
            with self.assertRaises(SessionRuntimeError):
                runtime.switch(self.second.id, confirm=lambda _: True)
            self.assertFalse(runtime.close())
            self.assert_busy(self.workspace)
        finally:
            resource.reaped = True
            runtime.close()
        self.runtime()

    def test_cli_enters_unbound_landing_while_existing_session_is_busy(self):
        from tricoder.cli import main
        self.runtime()
        output = io.StringIO()
        def forbidden_provider(*args, **kwargs):
            self.fail("入口不应构建 Provider")
        def shell_factory(runtime, _ui, *, input_fn):
            self.assertIsNone(runtime.current)
            return type("Shell", (), {"run": lambda _self: 0})()
        result = main(
            ["chat", "--workspace", str(self.workspace), "--no-color"],
            environ={}, output=output, provider_factory=forbidden_provider, shell_factory=shell_factory,
            session_store_factory=lambda _: self.store,
        )
        self.assertEqual(0, result)
        self.assertNotIn("占用", output.getvalue())

    def test_cli_initialization_of_shell_failure_releases_runtime(self):
        from tricoder.cli import main
        runtime = self.runtime()
        def fail_shell(*args, **kwargs):
            raise ValueError("synthetic shell failure")
        with mock.patch("tricoder.cli.SessionRuntime", return_value=runtime):
            with self.assertRaisesRegex(ValueError, "synthetic shell failure"):
                main(["chat", "--workspace", str(self.workspace)], environ={},
                     session_store_factory=lambda _: self.store, shell_factory=fail_shell)
        self.runtime()

    def test_tui_reports_busy_session_and_preserves_existing_owner(self):
        from tricoder.presentation.tui import TricoderApp
        self.runtime()
        async def check():
            app = TricoderApp(lambda *_: self.runtime())
            async with app.run_test() as pilot:
                await pilot.pause()
            self.assertEqual(2, app.return_value)
            self.assertTrue(any("该会话已被其他终端占用" in line for line in app._lines))
        asyncio.run(check())
        self.assert_busy(self.workspace)

    def test_tui_unmount_releases_idle_session(self):
        from tricoder.presentation.tui import TricoderApp
        runtime = self.runtime()
        async def check():
            app = TricoderApp(lambda *_: runtime)
            async with app.run_test() as pilot:
                await pilot.pause()
        asyncio.run(check())
        self.runtime()

    def test_close_releases_lock_and_stale_runtime_cannot_write(self):
        runtime = self.runtime()
        self.assertTrue(runtime.close())
        peer = self.runtime()
        peer.rename_current("peer-owned")
        with self.assertRaises(SessionRuntimeError):
            runtime.rename_current("stale-owner")
        with self.assertRaises(SessionRuntimeError):
            runtime.run_task("stale-task")
        self.assertEqual("peer-owned", self.store.get(self.first.id).name)
        self.assertTrue(runtime.close())

    def test_close_during_task_retains_lock_until_task_has_finished(self):
        from tricoder.models import RunResult, SessionTurnResult
        started, finish = threading.Event(), threading.Event()
        class WaitingAgent:
            def run_with_context(self, task, context, **kwargs):
                started.set()
                if not finish.wait(5):
                    raise TimeoutError("test worker not released")
                return SessionTurnResult(RunResult(False, "cancelled", 0), context)
        runtime = self.runtime()
        runtime.current = replace(runtime.current, agent=WaitingAgent())
        worker = threading.Thread(target=lambda: runtime.run_task("wait"))
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertFalse(runtime.close())
            self.assert_busy(self.workspace)
        finally:
            finish.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.runtime()

    def test_close_racing_with_idle_operation_return_is_not_lost(self):
        runtime = self.runtime()
        releasing, allow_release, close_started, close_done = (threading.Event() for _ in range(4))
        original_lock = runtime._task_lock
        errors = []
        class ReleaseGate:
            def acquire(self, **kwargs):
                return original_lock.acquire(**kwargs)
            def release(self):
                if threading.current_thread() is writer:
                    releasing.set()
                    if not allow_release.wait(3):
                        raise TimeoutError("test did not release writer")
                original_lock.release()
        runtime._task_lock = ReleaseGate()
        def rename():
            try:
                runtime.rename_current("renamed")
            except BaseException as exc:
                errors.append(exc)
        def close():
            close_started.set()
            try:
                runtime.close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_done.set()
        writer = threading.Thread(target=rename)
        closer = threading.Thread(target=close)
        writer.start()
        try:
            self.assertTrue(releasing.wait(3))
            closer.start()
            self.assertTrue(close_started.wait(3))
            # 旧实现此时丢弃 close 请求；正确实现将持状态锁完成所有权交接。
            close_done.wait(0.1)
        finally:
            allow_release.set()
            writer.join(3)
            if closer.ident is not None:
                closer.join(3)
        self.assertFalse(writer.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual([], errors)
        self.runtime()

    def child(self):
        return self._spawn_child("--hold")

    def _spawn_child(self, mode):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen(
            [sys.executable, "-u", __file__, mode, str(self.store.database_path), str(self.workspace)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment,
        )
        def stop():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        # 不把失败子进程的原始 stderr 当作用户数据；只运行合成 fixture。
        ready = threading.Event()
        lines = []
        def read_ready():
            lines.append(process.stdout.readline())
            ready.set()
        threading.Thread(target=read_ready, daemon=True).start()
        self.assertTrue(ready.wait(5), "child did not start within deadline")
        self.assertTrue(lines[0].startswith("ready"), lines)
        return process

    def _read_child_line(self, process):
        ready = threading.Event()
        lines = []
        def read_line():
            lines.append(process.stdout.readline())
            ready.set()
        threading.Thread(target=read_line, daemon=True).start()
        self.assertTrue(ready.wait(5), "child did not answer within deadline")
        return lines[0]

    def test_two_processes_wait_in_entry_without_rows_or_locks(self):
        before = tuple(self.store.list_all())
        first = self._spawn_child("--entry")
        second = self._spawn_child("--entry")

        owner = self.runtime()
        self.assertEqual(self.first.id, owner.current.record.id)
        self.assertEqual(before, tuple(self.store.list_all()))

        first.communicate("exit\n", timeout=5)
        second.communicate("exit\n", timeout=5)
        self.assertEqual((0, 0), (first.returncode, second.returncode))

    def test_two_processes_first_submit_compete_for_one_workspace_task(self):
        before = len(self.store.list_all())
        first = self._spawn_child("--submit")
        second = self._spawn_child("--submit")

        for process in (first, second):
            process.stdin.write("go\n")
            process.stdin.flush()
        outcomes = (self._read_child_line(first).strip(), self._read_child_line(second).strip())

        self.assertEqual(1, sum(item.startswith("activated:") for item in outcomes))
        self.assertEqual(1, sum(item == "workspace-busy" for item in outcomes))
        self.assertEqual(before + 1, len(self.store.list_all()))
        for process, outcome in zip((first, second), outcomes):
            if outcome.startswith("activated:"):
                process.communicate("exit\n", timeout=5)
                self.assertEqual(0, process.returncode)
            else:
                process.communicate(timeout=5)
                self.assertEqual(3, process.returncode)

    def test_historical_default_is_a_normal_locked_session(self):
        record = self.store.create("default", self.workspace, "openai", "legacy")
        owner = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=build_session,
            initial_session_id=record.id,
        )
        self.addCleanup(owner.close)
        with self.assertRaisesRegex(SessionRuntimeError, "占用"):
            SessionRuntime(
                self.store,
                self.workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=build_session,
                initial_session_id=record.id,
            )

    def test_other_process_holds_lock_and_normal_exit_releases_it(self):
        child = self.child()
        self.assert_busy(self.workspace)
        child.communicate("exit\n", timeout=5)
        self.assertEqual(0, child.returncode)
        self.runtime()

    def test_killed_process_does_not_leave_a_stale_lock(self):
        child = self.child()
        self.assert_busy(self.workspace)
        child.kill()
        child.communicate(timeout=5)
        self.runtime()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"--hold", "--entry", "--submit"}:
        mode = sys.argv[1]
        store = SessionStore(Path(sys.argv[2]))
        workspace = Path(sys.argv[3])
        initial_session_id = None
        if mode == "--hold":
            initial_session_id = store.latest_for_workspace(workspace.resolve()).id
        runtime = SessionRuntime(
            store,
            workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=(build_slow_session if mode == "--submit" else build_session),
            initial_session_id=initial_session_id,
        )
        print("ready", flush=True)
        input()
        if mode == "--submit":
            try:
                runtime.run_task("same synthetic task")
            except SessionRuntimeError as exc:
                if "工作区正在执行其他任务" not in str(exc):
                    raise
                print("workspace-busy", flush=True)
                runtime.close()
                raise SystemExit(3)
            print(f"activated:{runtime.current.record.id}", flush=True)
            input()
        runtime.close()
    else:
        unittest.main()
