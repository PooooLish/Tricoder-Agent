"""工作区级互斥锁的进程与路径安全边界。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.models import (
    AppConfig,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionTurnResult,
)
from tricoder.policy import PolicyError, WorkspacePolicy
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore
from tricoder.task_cleanup import current_cleanup
from tricoder.workspace.lock import (
    WorkspaceIdentityError,
    WorkspaceLock,
    WorkspaceLockBusyError,
    WorkspaceRecoveryRequiredError,
)


class WorkspaceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.other = self.root / "other"
        self.workspace.mkdir()
        self.other.mkdir()

    def test_same_process_cannot_acquire_same_workspace_twice_and_lock_file_remains(self) -> None:
        """若同进程第二个实例被放行，同一工作区可同时执行两个任务。"""

        first = WorkspaceLock.acquire(self.workspace)
        self.addCleanup(first.close)

        with self.assertRaises(WorkspaceLockBusyError):
            WorkspaceLock.acquire(self.workspace)

        lock_path = first.lock_path
        self.assertTrue(lock_path.is_file())
        self.assertTrue(first.guard_path.is_file())

    @unittest.skipIf(os.name == "nt", "Windows 打开锁文件时控制目录不能被重命名")
    def test_replacing_internal_control_directory_cannot_create_second_lock(self) -> None:
        """POSIX 内部锁 inode 被移走后，外部守卫仍阻止第二个合规进程。"""

        ownership = WorkspaceLock.acquire(self.workspace)
        control = ownership.lock_path.parent
        moved = control.with_name("tricoder-control-moved")
        try:
            control.rename(moved)
            control.mkdir()
            with self.assertRaises(WorkspaceLockBusyError):
                WorkspaceLock.acquire(self.workspace)
        finally:
            ownership.close()
        second = WorkspaceLock.acquire(self.workspace)
        second.close()

    @unittest.skipIf(os.name == "nt", "Windows 打开锁文件时不能替换守卫文件")
    def test_guard_path_replacement_is_detected_before_next_lifecycle_boundary(self) -> None:
        """外部守卫的名称若改指向新 inode，当前持有者必须停止继续执行。"""

        ownership = WorkspaceLock.acquire(self.workspace)
        guard = ownership.guard_path
        moved = guard.with_name(f"{guard.name}.moved")
        try:
            guard.rename(moved)
            guard.write_text("replacement\n", encoding="utf-8")
            with self.assertRaises(WorkspaceIdentityError):
                ownership.ensure_workspace_stable()
        finally:
            guard.unlink(missing_ok=True)
            moved.rename(guard)
            ownership.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "需要 Linux 抽象 socket 锁")
    def test_replacing_both_lock_names_cannot_split_kernel_lock_domain(self) -> None:
        """即使两个文件系统锁都被移出名称空间，第二个合规进程仍不能进入。"""

        ownership = WorkspaceLock.acquire(self.workspace)
        guard = ownership.guard_path
        moved_guard = guard.with_name(f"{guard.name}.split-test")
        control = ownership.lock_path.parent
        moved_control = control.with_name("tricoder-control-split-test")
        try:
            guard.rename(moved_guard)
            guard.write_text("replacement\n", encoding="utf-8")
            control.rename(moved_control)
            control.mkdir()

            with self.assertRaises(WorkspaceLockBusyError):
                WorkspaceLock.acquire(self.workspace)
        finally:
            guard.unlink(missing_ok=True)
            moved_guard.rename(guard)
            control.rmdir()
            moved_control.rename(control)
            ownership.close()

    def test_guard_root_is_stable_per_user_and_not_derived_from_temp(self) -> None:
        """不同 TMP 配置不能让同一用户为同一工作区派生不同守卫锁。"""

        ownership = WorkspaceLock.acquire(self.workspace)
        self.addCleanup(ownership.close)

        self.assertEqual(
            Path.home() / ".tricoder" / "workspace-locks-v1",
            ownership.guard_path.parent,
        )

    def test_control_directory_identity_failure_releases_every_guard(self) -> None:
        """控制目录校验失败后不得泄漏外部锁或内核锁。"""

        with patch(
            "tricoder.workspace.lock._ensure_plain_directory",
            side_effect=WorkspaceIdentityError("synthetic identity failure"),
        ):
            with self.assertRaises(WorkspaceIdentityError):
                WorkspaceLock.acquire(self.workspace)

        recovered = WorkspaceLock.acquire(self.workspace)
        recovered.close()

    def test_child_process_blocks_same_workspace_but_not_other_workspace(self) -> None:
        """若锁身份依赖 Session/数据库，真实子进程会错误取得第二把锁。"""

        child = self._spawn_holder(self.workspace)
        with self.assertRaises(WorkspaceLockBusyError):
            WorkspaceLock.acquire(self.workspace)

        other = WorkspaceLock.acquire(self.other)
        other.close()
        child.communicate("exit\n", timeout=5)
        self.assertEqual(0, child.returncode)

        recovered = WorkspaceLock.acquire(self.workspace)
        recovered.close()

    def test_control_directory_is_reserved_from_builtin_workspace_access(self) -> None:
        """若模型工具能访问控制目录，就能读取或破坏锁与活动标记。"""

        lock = WorkspaceLock.acquire(self.workspace)
        self.addCleanup(lock.close)
        policy = WorkspacePolicy(self.workspace)

        with self.assertRaises(PolicyError):
            policy.resolve_path("runtime/tricoder-control")
        with self.assertRaises(PolicyError):
            policy.resolve_path("runtime/tricoder-control/workspace.lock")

    def test_reparse_workspace_or_control_directory_is_rejected(self) -> None:
        """若锁路径跟随链接，两个拼写可落到不同锁文件并绕过互斥。"""

        target = self.root / "target"
        target.mkdir()
        linked = self.root / "linked"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("当前 Windows 权限不允许创建目录链接")
        with self.assertRaises(WorkspaceIdentityError):
            WorkspaceLock.acquire(linked)

        control_target = self.root / "control-target"
        control_target.mkdir()
        (self.workspace / "runtime").mkdir()
        control = self.workspace / "runtime" / "tricoder-control"
        control.symlink_to(control_target, target_is_directory=True)
        with self.assertRaises(WorkspaceIdentityError):
            WorkspaceLock.acquire(self.workspace)

    def test_activity_marker_survives_crash_and_requires_explicit_recovery(self) -> None:
        """若崩溃后只依赖 OS 解锁，未知子进程状态会被误当成已清理。"""

        child = self._spawn_holder(self.workspace, mode="--mark")
        child.kill()
        child.communicate(timeout=5)

        with self.assertRaises(WorkspaceRecoveryRequiredError):
            WorkspaceLock.acquire(self.workspace)
        with self.assertRaises(WorkspaceRecoveryRequiredError):
            WorkspaceLock.recover_stale_activity(self.workspace, confirmed=False)

        self.assertTrue(WorkspaceLock.recover_stale_activity(self.workspace, confirmed=True))
        recovered = WorkspaceLock.acquire(self.workspace)
        recovered.close()

    def _spawn_holder(
        self,
        workspace: Path,
        *,
        mode: str = "--hold",
    ) -> subprocess.Popen[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen(
            [sys.executable, "-u", __file__, mode, str(workspace)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        def stop() -> None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(stop)
        ready = threading.Event()
        lines: list[str] = []

        def read_ready() -> None:
            assert process.stdout is not None
            lines.append(process.stdout.readline())
            ready.set()

        threading.Thread(target=read_ready, daemon=True).start()
        self.assertTrue(ready.wait(5), "工作区锁子进程未在期限内启动")
        self.assertEqual("ready\n", lines[0])
        return process


class _WaitingAgent:
    def __init__(self, started: threading.Event, finish: threading.Event) -> None:
        self.started = started
        self.finish = finish
        self.calls = 0

    def run_with_context(self, task, context, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.started.set()
        if not self.finish.wait(5):
            raise TimeoutError("测试未释放等待中的 Agent")
        return SessionTurnResult(RunResult(True, "synthetic", 1), context)


class _ImmediateAgent:
    def __init__(self) -> None:
        self.calls = 0

    def run_with_context(self, task, context, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return SessionTurnResult(RunResult(True, "synthetic", 1), context)


def _builder(agent):  # type: ignore[no-untyped-def]
    def build(record, memory, _options):  # type: ignore[no-untyped-def]
        config = AppConfig(
            workspace=record.workspace,
            provider=ProviderConfig("openai", "synthetic", "https://example.invalid", "model"),
        )
        return ActiveSession(
            record,
            memory,
            SessionContext(persisted_summary=memory.summary),
            config,
            agent,
        )

    return build


class WorkspaceRuntimeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store_a = SessionStore(self.root / "state-a" / "sessions.db")
        self.store_b = SessionStore(self.root / "state-b" / "sessions.db")
        self.store_a.initialize(self.workspace)
        self.store_b.initialize(self.workspace)

    def _runtime(self, store: SessionStore, agent) -> SessionRuntime:  # type: ignore[no-untyped-def]
        runtime = SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(agent),
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_different_databases_compete_before_second_session_or_provider_call(self) -> None:
        """若 Runtime 在建 Session 后才竞争，阻塞任务会留下行或调用 Agent。"""

        started, finish = threading.Event(), threading.Event()
        first_agent = _WaitingAgent(started, finish)
        second_agent = _ImmediateAgent()
        first = self._runtime(self.store_a, first_agent)
        second = self._runtime(self.store_b, second_agent)
        outcomes: list[object] = []
        worker = threading.Thread(target=lambda: outcomes.append(first.run_task("first")))
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            with self.assertRaisesRegex(SessionRuntimeError, "工作区正在执行其他任务"):
                second.run_task("second")
            self.assertEqual(0, second_agent.calls)
            self.assertEqual([], self.store_b.list_all())
        finally:
            finish.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, first_agent.calls)
        self.assertEqual(1, len(outcomes))

        result = second.run_task("after-release")
        self.assertTrue(result.ok)
        self.assertEqual(1, second_agent.calls)

    def test_pending_cleanup_retains_workspace_lock_until_retry_confirms_cleanup(self) -> None:
        """若任务返回时提前解锁，旧资源仍活动时另一会话可以接管工作区。"""

        class Resource:
            reaped = False

            def cleanup(self, _deadline):  # type: ignore[no-untyped-def]
                return self.reaped

        resource = Resource()

        class DirtyCleanupAgent(_ImmediateAgent):
            def run_with_context(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
                cleanup = current_cleanup()
                assert cleanup is not None
                cleanup.retain(resource)
                return super().run_with_context(task, context, **kwargs)

        owner = self._runtime(self.store_a, DirtyCleanupAgent())
        peer_agent = _ImmediateAgent()
        peer = self._runtime(self.store_b, peer_agent)

        owner.run_task("leave-resource")
        with self.assertRaisesRegex(SessionRuntimeError, "工作区正在执行其他任务"):
            peer.run_task("must-wait")
        self.assertEqual(0, peer_agent.calls)

        resource.reaped = True
        self.assertTrue(owner.cleanup_pending_resources())
        self.assertTrue(peer.run_task("after-cleanup").ok)

    def test_cancelled_task_releases_workspace_lock_when_cleanup_is_confirmed(self) -> None:
        """若异常路径泄漏锁，后续独立 Runtime 将永久被拒绝。"""

        class FailingAgent(_ImmediateAgent):
            def run_with_context(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                raise KeyboardInterrupt()

        owner = self._runtime(self.store_a, FailingAgent())
        peer = self._runtime(self.store_b, _ImmediateAgent())
        with self.assertRaises(KeyboardInterrupt):
            owner.run_task("interrupt")
        self.assertTrue(peer.run_task("recover").ok)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] in {"--hold", "--mark"}:
        held = WorkspaceLock.acquire(Path(sys.argv[2]))
        if sys.argv[1] == "--mark":
            held.mark_active()
        print("ready", flush=True)
        input()
        held.close()
    else:
        unittest.main()
