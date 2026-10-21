"""Real-process regression coverage for bounded process-tree cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.process.control import run_bounded_process
from tricoder.task_cleanup import TaskCleanup, cleanup_scope


class BoundedProcessTests(unittest.TestCase):
    def test_readonly_timeout_opt_in_is_local_but_default_still_terminates(self):
        for options in ({}, {"recoverable_timeout": True}):
            with self.subTest(options=options):
                scope = TaskCleanup()
                with cleanup_scope(scope):
                    result = run_bounded_process(
                        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(10)"],
                        cwd=self.root, env=dict(os.environ), timeout=.2,
                        max_output_bytes=100, stdin_bytes=b"x" * 1000000, **options,
                    )
                self.assertTrue(result.timed_out)
                self.assertFalse(result.cleanup_failed)
                self.assertIsNone(result.cleanup_resource)
                self.assertFalse(scope.has_pending)
                self.assertFalse(scope.failed)
                if options:
                    self.assertIsNone(scope.started_deadline)
                else:
                    self.assertIsNotNone(scope.started_deadline)

    def test_cleanup_exception_does_not_replace_original_business_exception(self):
        from tricoder.process import control
        primary = ValueError("primary synthetic")
        process = mock.Mock(returncode=None)
        with mock.patch.object(control.subprocess, "Popen", return_value=process), \
             mock.patch.object(control, "_create_windows_job", return_value=mock.Mock()), \
             mock.patch.object(control, "_collect_bounded_process", side_effect=primary), \
             mock.patch.object(control, "_terminate_process_tree", side_effect=RuntimeError("cleanup synthetic")):
            with self.assertRaises(ValueError) as caught:
                run_bounded_process(["synthetic"], cwd=self.root, env={}, timeout=1, max_output_bytes=100)
        self.assertIs(primary, caught.exception)

    def test_failed_job_close_keeps_handle_for_owner_retry(self):
        from tricoder.process.control import _WindowsJob
        closed = []
        outcomes = iter((False, True))
        def close(handle):
            closed.append(handle)
            return next(outcomes)
        job = _WindowsJob(123, close)
        self.assertFalse(job.close())
        self.assertTrue(job.close())
        self.assertEqual([123, 123], closed)

    @unittest.skipUnless(os.name == "nt", "Windows 句柄退出证据；POSIX 进程组须在对应平台复验")
    def test_event_driven_cancellation_reaps_parent_and_both_descendant_kinds(self):
        import ctypes
        from ctypes import wintypes
        from tricoder.process import control

        ready = threading.Event()
        pids = []
        failures = []
        popen = control.subprocess.Popen
        token = CancellationToken()
        child_code = "import threading; threading.Event().wait()"
        parent_code = (
            "import subprocess,sys,threading,os; "
            f"a=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"b=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
            "print(os.getpid(),a.pid,b.pid,flush=True); threading.Event().wait()"
        )

        class ObservedStream:
            def __init__(self, stream):
                self.stream = stream
            def read1(self, amount):
                chunk = self.stream.read1(amount)
                if chunk:
                    pids.extend(int(value) for value in chunk.split())
                    ready.set()
                return chunk
            def close(self):
                self.stream.close()

        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            process.stdout = ObservedStream(process.stdout)
            return process

        def run():
            try:
                run_bounded_process([sys.executable, "-c", parent_code], cwd=self.root,
                    env=dict(os.environ), timeout=10, max_output_bytes=4096, cancellation=token)
            except BaseException as exc:
                failures.append(exc)

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = []
        with mock.patch.object(control.subprocess, "Popen", side_effect=capture):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(ready.wait(3), "受管父进程尚未报告子进程")
                self.assertEqual(3, len(pids))
                handles = [kernel.OpenProcess(0x00100000, False, pid) for pid in pids]
                self.assertTrue(all(handles))
                started = time.monotonic()
                token.cancel()
                worker.join(1)
                self.assertFalse(worker.is_alive(), "调用方取消反馈超过 1 秒目标")
                self.assertEqual(1, len(failures))
                self.assertIsInstance(failures[0], CancellationError)
                self.assertFalse(failures[0].cleanup_failed)
                # 调用方返回与资源退出分别观察，句柄 signaled 才是退出证据。
                deadline = started + 7
                for handle in handles:
                    self.assertEqual(0, kernel.WaitForSingleObject(handle, max(0, int((deadline-time.monotonic())*1000))))
            finally:
                token.cancel()
                worker.join(7)
                for handle in handles:
                    if handle:
                        kernel.CloseHandle(handle)

    def test_cleanup_uses_one_deadline_for_tree_and_both_readers(self):
        from tricoder.process import control
        clock = [0.0]
        budgets = []
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0

        def terminate(*args, **kwargs):
            clock[0] += 4.0
            return True

        def join(timeout):
            budgets.append(timeout)
            clock[0] += timeout

        reader = SimpleNamespace(start=lambda: None, join=join, is_alive=lambda: True)
        with mock.patch.object(control.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(control, "_terminate_process_tree", side_effect=terminate), \
             mock.patch.object(control.threading, "Thread", return_value=reader):
            result = control._collect_bounded_process(process, env={}, timeout=30,
                                                     max_output_bytes=4096, windows_job=None)
        self.assertTrue(result.cleanup_failed)
        self.assertLessEqual(sum(budgets), 1.0, "树清理耗费后，每个 reader 不能重获完整预算")

    def test_cancel_cleanup_failure_keeps_cancellation_primary(self):
        from tricoder.process import control
        token = CancellationToken()
        token.cancel()
        process = mock.Mock(returncode=None)
        process.poll.return_value = None
        reader = SimpleNamespace(start=lambda: None, join=lambda timeout: None,
                                 is_alive=lambda: False)
        with mock.patch.object(control, "_terminate_process_tree", return_value=False), \
             mock.patch.object(control.threading, "Thread", return_value=reader):
            with self.assertRaises(CancellationError) as caught:
                control._collect_bounded_process(process, env={}, timeout=30,
                    max_output_bytes=4096, windows_job=None, cancellation=token)
        self.assertTrue(getattr(caught.exception, "cleanup_failed", False))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cancellation_terminates_running_process_before_side_effect(self) -> None:
        """取消命令时必须终止受管进程树，并以主动取消而非超时结束。"""
        marker = self.root / "cancelled-process-survived.txt"
        script = self.root / "cancellable.py"
        script.write_text(
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(1.0)\n"
            f"Path({str(marker)!r}).write_text('alive')\n",
            encoding="utf-8",
        )
        token = CancellationToken()
        timer = threading.Timer(0.15, token.cancel)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(CancellationError):
                run_bounded_process(
                    [sys.executable, str(script)],
                    cwd=self.root,
                    env=dict(os.environ),
                    timeout=5,
                    max_output_bytes=4096,
                    cancellation=token,
                )
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        time.sleep(1.1)

        self.assertLess(elapsed, 1.0)
        self.assertFalse(marker.exists())

    def test_normal_parent_exit_cleans_delayed_descendant(self) -> None:
        """A zero-exit session leader must not orphan a delayed child."""
        child_code = (
            "import time; from pathlib import Path; "
            "time.sleep(0.6); Path('normal-child-alive.txt').write_text('alive')"
        )
        parent = self.root / "normal_parent.py"
        parent.write_text(
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL)\n",
            encoding="utf-8",
        )

        result = run_bounded_process(
            [sys.executable, str(parent)],
            cwd=self.root,
            env=dict(os.environ),
            timeout=5,
            max_output_bytes=4096,
        )
        time.sleep(0.9)

        self.assertEqual(0, result.returncode)
        self.assertFalse(result.cleanup_failed)
        self.assertFalse((self.root / "normal-child-alive.txt").exists())

    def test_cap_plus_one_terminates_tree_before_delayed_marker(self) -> None:
        """刚超过上限的少量输出也必须立即触发进程树清理。"""

        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream=stream_name):
                marker = self.root / f"{stream_name}-after-overflow.txt"
                script = self.root / f"{stream_name}_overflow.py"
                script.write_text(
                    "import sys, time\n"
                    "from pathlib import Path\n"
                    f"stream = sys.{stream_name}.buffer\n"
                    "stream.write(b'x' * 4097)\n"
                    "stream.flush()\n"
                    "time.sleep(0.8)\n"
                    f"Path({str(marker)!r}).write_text('alive')\n"
                    "time.sleep(10)\n",
                    encoding="utf-8",
                )

                started = time.monotonic()
                result = run_bounded_process(
                    [sys.executable, str(script)],
                    cwd=self.root,
                    env=dict(os.environ),
                    timeout=3,
                    max_output_bytes=4096,
                )
                elapsed = time.monotonic() - started
                time.sleep(1.0)

                self.assertTrue(result.output_exceeded)
                self.assertFalse(result.timed_out)
                self.assertLess(elapsed, 2.0)
                self.assertFalse(marker.exists())


class RecoverableCleanupBudgetTests(unittest.TestCase):
    """用受控时钟与清理回调核验期限，避免调度速度决定安全断言。"""

    def setUp(self):
        from tricoder.process import control
        self.control = control
        self.clock = 100.0
        self.scope = TaskCleanup()
        self.token = CancellationToken()
        self.process = mock.Mock(returncode=None)
        self.process.poll.return_value = None
        self.resources = control._ProcessResources(self.process, {})
        reader = SimpleNamespace(start=lambda: None, join=lambda timeout: None,
                                 is_alive=lambda: False)
        for target in (mock.patch.object(control.time, "monotonic", side_effect=lambda: self.clock),
                       mock.patch.object(control.threading, "Thread", return_value=reader)):
            target.start()
            self.addCleanup(target.stop)

    def collect(self, *, recoverable_timeout=True):
        with cleanup_scope(self.scope):
            return self.control._collect_bounded_process(
                self.process, env={}, timeout=0, max_output_bytes=100, windows_job=None,
                cancellation=self.token, resources=self.resources,
                recoverable_timeout=recoverable_timeout,
            )

    def test_local_timeout_cleanup_has_bounded_budget_without_task_deadline(self):
        deadlines = []
        with mock.patch.object(self.control, "_terminate_process_tree",
                               side_effect=lambda *a, **kw: deadlines.append(kw["deadline"]) or True):
            result = self.collect()
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cleanup_failed)
        self.assertEqual([105.0], deadlines)
        self.assertIsNone(self.scope.started_deadline)
        self.assertFalse(self.scope.has_pending)

    def test_local_failure_promotes_consumed_deadline_and_retains_exact_owner(self):
        def unconfirmed(*args, **kwargs):
            self.assertEqual(105.0, kwargs["deadline"])
            self.clock = 104.0
            return False

        with mock.patch.object(self.control, "_terminate_process_tree", side_effect=unconfirmed):
            result = self.collect()
        self.assertTrue(result.timed_out)
        self.assertTrue(result.cleanup_failed)
        self.assertIs(self.resources, result.cleanup_resource)
        self.assertEqual(105.0, self.scope.started_deadline)
        self.assertTrue(self.scope.failed)
        self.assertTrue(self.scope.has_pending)
        owner = TaskCleanup()
        self.scope.handoff_to(owner)
        self.assertTrue(owner.blocks(self.scope))
        with mock.patch.object(self.resources, "cleanup", return_value=True) as retry:
            self.assertTrue(self.scope.retry(110.0))
        retry.assert_called_once_with(110.0)
        self.assertFalse(self.scope.has_pending)
        self.assertFalse(owner.has_pending)
        self.assertTrue(self.scope.failed)
        self.assertEqual(105.0, self.scope.started_deadline)

    def test_existing_termination_deadline_is_inherited_even_when_expired(self):
        for deadline in (101.0, 99.0):
            with self.subTest(deadline=deadline):
                self.scope = TaskCleanup()
                self.resources = self.control._ProcessResources(self.process, {})
                self.scope.begin_termination(deadline=deadline)
                with mock.patch.object(self.control, "_terminate_process_tree", return_value=True) as terminate:
                    self.collect()
                self.assertEqual(deadline, terminate.call_args.kwargs["deadline"])
                self.assertEqual(deadline, self.scope.started_deadline)

    def test_opt_in_does_not_clear_existing_failed_or_pending_facts(self):
        previous = mock.Mock()
        self.scope.mark_failed()
        self.scope.retain(previous)
        with mock.patch.object(self.control, "_terminate_process_tree", return_value=True):
            self.collect()
        self.assertTrue(self.scope.failed)
        self.assertTrue(self.scope.has_pending)
        previous.cleanup.return_value = True
        self.assertTrue(self.scope.retry(105.0))
        previous.cleanup.assert_called_once_with(105.0)
        self.assertTrue(self.scope.failed)

    def test_execution_cancel_still_terminates_when_timeout_is_recoverable(self):
        self.token.cancel()
        with mock.patch.object(self.control, "_terminate_process_tree", return_value=True):
            with self.assertRaises(CancellationError) as caught:
                self.collect()
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertEqual(105.0, self.scope.started_deadline)
        self.assertFalse(self.scope.has_pending)

    def test_cancellation_during_cleanup_wins_over_timeout_and_keeps_consumed_budget(self):
        for recoverable in (False, True):
            for cleanup_ok in (False, True):
                with self.subTest(recoverable=recoverable, cleanup_ok=cleanup_ok):
                    self.scope = TaskCleanup()
                    self.token = CancellationToken()
                    self.resources = self.control._ProcessResources(self.process, {})
                    self.clock = 100.0

                    def cancel_during_cleanup(*args, **kwargs):
                        self.clock = 102.0
                        self.token.cancel()
                        return cleanup_ok

                    try:
                        with mock.patch.object(self.control, "_terminate_process_tree", side_effect=cancel_during_cleanup):
                            with self.assertRaises(CancellationError) as caught:
                                self.collect(recoverable_timeout=recoverable)
                        self.assertEqual(not cleanup_ok, caught.exception.cleanup_failed)
                        self.assertIs(self.resources, caught.exception.cleanup_resource)
                        self.assertEqual(105.0, self.scope.started_deadline)
                        self.assertEqual(not cleanup_ok, self.scope.has_pending)
                        self.assertEqual(not cleanup_ok, self.scope.failed)
                    finally:
                        with mock.patch.object(self.resources, "cleanup", return_value=True):
                            self.assertTrue(self.scope.retry(110.0))


if __name__ == "__main__":
    unittest.main()
