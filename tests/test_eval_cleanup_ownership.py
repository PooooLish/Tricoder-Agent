"""Eval must own failed verifier resources, independently of safe score data."""
import gc
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
import unittest
import weakref
from unittest.mock import patch

from tricoder.evals import runner
from tricoder.evals.models import VerificationSpec
from tricoder.process.control import BoundedProcessResult, ProcessExecutionUncertain
from tricoder.task_cleanup import TaskCleanup, cleanup_scope, current_cleanup
from tricoder.evals.cleanup import (verification_owner_scope, retry_verification_cleanup,
                                   verification_cleanup_owner)
from tricoder.core.cancellation import CancellationError


class Resource:
    def __init__(self, calls, deadline, closed=False):
        self.calls = calls
        self.deadline = deadline
        self.closed = closed

    def cleanup(self, deadline):
        self.calls.append(deadline)
        return self.closed


class EvalCleanupOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'check.py').write_text('pass\n', encoding='utf8')
        self.spec = VerificationSpec('hidden', 'python check.py', 5)

    def reproduce(self, mode, *, closed=False, parent=None):
        refs, calls = [], []
        deadline = time.monotonic() - 1
        def process(*args, **kwargs):
            resource = Resource(calls, deadline, closed)
            refs.append(weakref.ref(resource))
            if mode == 'exception':
                raise ProcessExecutionUncertain(cleanup_failed=True, cleanup_resource=resource)
            return BoundedProcessResult(None, 'PRIVATE OUTPUT', 'PRIVATE ERROR',
                                        cleanup_failed=True, cleanup_resource=resource)
        # Use the existing ambient owner API: regression is not a missing-new-API error.
        scope = parent or TaskCleanup()
        with cleanup_scope(scope), patch.object(runner, 'run_bounded_process', side_effect=process):
            result = runner._run_verification(self.spec, self.root)
        gc.collect()
        self.assertEqual('verification_error', result.error_code)
        self.assertFalse(result.passed)
        self.assertNotIn('PRIVATE', json.dumps(asdict(result)))
        return scope, refs, calls, deadline, result

    def test_returned_resource_is_retained_and_retried_with_consumed_deadline(self):
        scope, refs, calls, deadline, _ = self.reproduce('result')
        self.assertTrue(scope.has_pending, 'returned cleanup_resource was dropped')
        self.assertIsNotNone(refs[0]())
        self.assertEqual([deadline], calls)
        self.assertTrue(scope.failed)
        self.assertEqual(deadline, scope.started_deadline)

    def test_exception_resource_is_retained_and_retried_with_consumed_deadline(self):
        scope, refs, calls, deadline, _ = self.reproduce('exception')
        self.assertTrue(scope.has_pending, 'exception cleanup_resource was dropped')
        self.assertIsNotNone(refs[0]())
        self.assertEqual([deadline], calls)
        self.assertTrue(scope.failed)
        self.assertEqual(deadline, scope.started_deadline)

    def test_confirmed_cleanup_keeps_failure_score_but_releases_resource(self):
        for mode in ('result', 'exception'):
            with self.subTest(mode=mode):
                scope, refs, calls, deadline, _ = self.reproduce(mode, closed=True)
                self.assertEqual([deadline], calls)
                self.assertFalse(scope.has_pending)
                self.assertTrue(scope.failed)
                self.assertIsNone(refs[0]())

    def test_existing_earlier_deadline_is_never_extended(self):
        scope = TaskCleanup()
        deadline = scope.begin_termination(deadline=time.monotonic()-30)
        _, _, calls, _, _ = self.reproduce('exception', parent=scope)
        self.assertEqual([deadline], calls)
        self.assertEqual(deadline, scope.started_deadline)

    def test_standalone_paths_handoff_block_and_release_exact_resource(self):
        for mode in ('result', 'exception'):
            with self.subTest(mode=mode):
                owner = TaskCleanup()
                calls, refs = [], []
                deadline = time.monotonic()-1
                def process(*args, **kwargs):
                    resource = Resource(calls, deadline)
                    refs.append(weakref.ref(resource))
                    if mode == 'exception':
                        raise ProcessExecutionUncertain(cleanup_failed=True, cleanup_resource=resource)
                    return BoundedProcessResult(None, '', '', cleanup_failed=True, cleanup_resource=resource)
                with verification_owner_scope(owner):
                    with patch.object(runner, 'run_bounded_process', side_effect=process):
                        result = runner._run_verification(self.spec, self.root)
                    gc.collect()
                    self.assertTrue(owner.has_pending)
                    self.assertIsNotNone(refs[0]())
                    self.assertTrue(result.diagnostics['resource_pending'])
                    # Pending cleanup must block before any new command is launched.
                    blocked = runner._run_verification(self.spec, self.root)
                    self.assertEqual('pending_cleanup', blocked.diagnostics['stage'])
                    self.assertFalse(retry_verification_cleanup())
                    self.assertEqual([deadline, deadline], calls)
                    refs[0]().closed = True
                    self.assertTrue(retry_verification_cleanup())
                    self.assertEqual([deadline]*3, calls)
                    self.assertFalse(owner.has_pending)
                    gc.collect()
                    self.assertIsNone(refs[0]())
                    self.assertTrue(runner._run_verification(self.spec, self.root).passed)

    def test_default_owner_survives_dropping_result_and_cleans_only_exact_resource(self):
        owner = TaskCleanup()
        with patch('tricoder.evals.cleanup._process_owner', owner):
            self.assertIs(owner, verification_cleanup_owner())
            resource = Resource([], time.monotonic()-1)
            response = BoundedProcessResult(None, '', '', cleanup_failed=True, cleanup_resource=resource)
            with patch.object(runner, 'run_bounded_process', return_value=response):
                runner._run_verification(self.spec, self.root)
            ref = weakref.ref(resource)
            del resource, response
            gc.collect()
            self.assertIsNotNone(ref())
            ref().closed = True
            self.assertTrue(retry_verification_cleanup())
            gc.collect()
            self.assertIsNone(ref())

    def test_cleanup_exception_is_retained_and_does_not_replace_failure(self):
        owner = TaskCleanup()
        resource = Resource([], time.monotonic()-1)
        def broken_cleanup(deadline):
            raise OSError('PRIVATE CLEANUP PATH')
        resource.cleanup = broken_cleanup
        with verification_owner_scope(owner), patch.object(runner, 'run_bounded_process',
                return_value=BoundedProcessResult(None, '', '', cleanup_failed=True, cleanup_resource=resource)):
            result = runner._run_verification(self.spec, self.root)
        self.assertEqual('verification_error', result.error_code)
        self.assertTrue(owner.has_pending)
        self.assertNotIn('PRIVATE', json.dumps(asdict(result)))

    def test_interrupt_retains_resource_and_preserves_exception_identity(self):
        owner = TaskCleanup()
        primary = KeyboardInterrupt('PRIVATE')
        resource = Resource([], time.monotonic()-1)
        primary.cleanup_resource = resource
        with verification_owner_scope(owner), patch.object(runner, 'run_bounded_process', side_effect=primary):
            with self.assertRaises(KeyboardInterrupt) as caught:
                runner._run_verification(self.spec, self.root)
        self.assertIs(primary, caught.exception)
        self.assertTrue(owner.has_pending)

    def test_secondary_cleanup_interrupt_preserves_primary_interrupt(self):
        owner = TaskCleanup()
        primary = KeyboardInterrupt('primary')
        resource = Resource([], time.monotonic()-1)
        def interrupted(deadline):
            raise KeyboardInterrupt('secondary cleanup')
        resource.cleanup = interrupted
        primary.cleanup_resource = resource
        with verification_owner_scope(owner), patch.object(runner, 'run_bounded_process', side_effect=primary):
            with self.assertRaises(KeyboardInterrupt) as caught:
                runner._run_verification(self.spec, self.root)
        self.assertIs(primary, caught.exception)
        self.assertTrue(owner.has_pending)

    def test_cancellation_keeps_existing_mapping_and_retains_resource(self):
        owner = TaskCleanup()
        primary = CancellationError('PRIVATE', cleanup_failed=True)
        primary.cleanup_resource = Resource([], time.monotonic()-1)
        with verification_owner_scope(owner), patch.object(runner, 'run_bounded_process', side_effect=primary):
            result = runner._run_verification(self.spec, self.root)
        self.assertEqual('verification_error', result.error_code)
        self.assertEqual('CancellationError', result.diagnostics['exception_type'])
        self.assertTrue(owner.has_pending)

    def test_diagnostics_are_bounded_and_do_not_expose_error_text_or_paths(self):
        cause = PermissionError(13, 'PRIVATE token=secret', '/PRIVATE/PATH')
        cause.winerror = 32
        primary = ProcessExecutionUncertain(cleanup_failed=False)
        primary.__cause__ = cause
        with patch.object(runner, 'run_bounded_process', side_effect=primary):
            result = runner._run_verification(self.spec, self.root)
        self.assertEqual('process_exception', result.diagnostics['stage'])
        self.assertEqual(32, result.diagnostics['winerror'])
        self.assertEqual(13, result.diagnostics['errno'])
        text = json.dumps(asdict(result))
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('secret', text)
        self.assertLess(len(text), 1000)

    def test_full_runner_hands_off_both_paths_and_blocks_next_case(self):
        from dataclasses import replace
        from tricoder.evals.loader import load_suite
        from tricoder.models import RunResult
        case = load_suite(Path(__file__).resolve().parents[1] / 'evals/quality-v3').cases[0]
        for mode in ('result', 'exception'):
            with self.subTest(mode=mode):
                owner = TaskCleanup()
                resource = Resource([], time.monotonic()-1)
                count = []
                def process(*args, **kwargs):
                    if mode == 'exception':
                        raise ProcessExecutionUncertain(cleanup_failed=True, cleanup_resource=resource)
                    return BoundedProcessResult(None, '', '', cleanup_failed=True, cleanup_resource=resource)
                def execute(*args):
                    count.append(1)
                    return RunResult(True, 'synthetic', 1, 0, (), '通过')
                trial = self.root / mode
                trial.mkdir()
                with patch.object(runner, 'run_bounded_process', side_effect=process):
                    result = runner.run_isolated_case(case, trial, execute, cleanup_owner=owner)
                self.assertEqual('error', result.status)
                self.assertIn('verification_error', result.failure_codes)
                self.assertTrue(owner.has_pending)
                second = self.root / (mode+'-second')
                second.mkdir()
                blocked = runner.run_isolated_case(case, second, execute, cleanup_owner=owner)
                self.assertEqual('error', blocked.status)
                self.assertIsNotNone(blocked.timings)
                self.assertEqual([1], count)
                # Safe report conversion must never attempt to copy locks/resources.
                from tricoder.evals.report import _case_as_dict
                json.dumps(_case_as_dict(result))
                json.dumps(asdict(result))
                resource.closed = True
                self.assertTrue(retry_verification_cleanup(owner))

    def test_job_unconfirmed_exit_keeps_handle_and_exact_retry_deadline(self):
        from tricoder.process.control import _WindowsJob
        closed, waited = [], []
        outcomes = iter((False, True))
        def wait(handle, deadline):
            waited.append((handle, deadline))
            return next(outcomes)
        job = _WindowsJob(123, lambda handle: closed.append(handle) or True, wait)
        self.assertFalse(job.close(deadline=17))
        self.assertEqual([], closed)
        self.assertEqual(123, job._handle)
        self.assertTrue(job.close(deadline=17))
        self.assertEqual([(123,17), (123,17)], waited)
        self.assertEqual([123], closed)
        self.assertTrue(job.close(deadline=17))
        self.assertEqual([123], closed)

    def test_inherited_pending_scope_blocks_executor_even_with_new_host_owner(self):
        from tricoder.evals.loader import load_suite
        from tricoder.models import RunResult
        case = load_suite(Path(__file__).resolve().parents[1] / 'evals/quality-v3').cases[0]
        parent = TaskCleanup()
        parent.retain(Resource([], time.monotonic()-1))
        calls = []
        def execute(*args):
            calls.append(1)
            return RunResult(True, 'synthetic', 1, 0, (), '通过')
        trial = self.root/'inherited'
        trial.mkdir()
        with cleanup_scope(parent):
            result = runner.run_isolated_case(case, trial, execute, cleanup_owner=TaskCleanup())
        self.assertEqual([], calls)
        self.assertEqual('error', result.status)
        self.assertTrue(parent.has_pending)

    @unittest.skipUnless(os.name == 'nt', 'Windows process handle and directory evidence')
    def test_real_overflow_reaps_parent_and_detached_child_before_directory_removal(self):
        import ctypes
        from ctypes import wintypes
        from tricoder.process import control
        from eval_process_diagnostics import ProcessDiagnostics
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        child = "import sys,threading;print('ready',flush=True);threading.Event().wait()"
        for attempt in range(10):
            with self.subTest(attempt=attempt), ProcessDiagnostics() as diagnostics:
                work = self.root / str(attempt)
                work.mkdir()
                (work/'check.py').write_text(
                    'import subprocess,sys\nfrom pathlib import Path\n'
                    f'p=subprocess.Popen([sys.executable,"-c",{child!r}],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)\n'
                    'assert p.stdout.readline().strip()==b"ready"\n'
                    'Path("child.pid").write_text(str(p.pid))\n'
                    'sys.stdout.write("x"*5000000);sys.stdout.flush()\n'
                    'Path("after-flood").write_text("bad")\n',encoding='utf8')
                handles = []
                finish = control._ProcessResources.finish
                def capture(resource, *args, **kwargs):
                    for pid in (resource.process.pid, int((work/'child.pid').read_text())):
                        handles.append(kernel.OpenProcess(0x00101000, False, pid))
                        member = wintypes.BOOL()
                        queried = kernel.IsProcessInJob(handles[-1], resource.windows_job._handle, ctypes.byref(member))
                        diagnostics.events.append({'stage': 'job_membership', 'pid': pid,
                                                   'queried': bool(queried), 'in_job': bool(member.value)})
                    return finish(resource, *args, **kwargs)
                try:
                    with patch.object(control._ProcessResources, 'finish', capture):
                        result = runner._run_verification(self.spec, work)
                    self.assertEqual('verification_error', result.error_code, result.diagnostics)
                    self.assertTrue(result.diagnostics['output_exceeded'], result.diagnostics)
                    self.assertFalse(result.diagnostics['cleanup_failed'], result.diagnostics)
                    self.assertEqual(2, len(handles))
                    self.assertTrue(all(handles))
                    for handle in handles:
                        self.assertEqual(0, kernel.WaitForSingleObject(handle, 0), diagnostics.events)
                    self.assertFalse((work/'after-flood').exists())
                    shutil.rmtree(work)
                    self.assertFalse(work.exists())
                finally:
                    for handle in handles:
                        if handle:
                            # Test-owned handle cleanup waits on actual exit, never
                            # delays the assertion or turns a failure into success.
                            kernel.WaitForSingleObject(handle, 5000)
                            kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
