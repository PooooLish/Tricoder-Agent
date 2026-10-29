"""Eval must own failed verifier resources, independently of safe score data."""
import gc
import json
import os
import shutil
import subprocess
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

    @unittest.skipUnless(os.name == 'nt', 'Windows native boundary')
    def test_native_capture_gap_stays_owned_through_eval_retry_and_blocks_executor(self):
        import ctypes
        from types import SimpleNamespace
        from test_windows_cleanup_boundaries import Kernel
        from tricoder.process import control
        from tricoder.evals.loader import load_suite
        case=load_suite(Path(__file__).resolve().parents[1]/'evals/quality-v3').cases[0]
        for failure in ('open_error','query_error','overflow'):
            for path in ('result','exception'):
                with self.subTest(failure=failure,path=path):
                    kernel=Kernel(failure)
                    self.addCleanup(kernel.dispose)
                    process=SimpleNamespace(pid=10,_handle=1010,stdin=None,stdout=None,stderr=None,
                        poll=lambda:0,wait=lambda **kw:0)
                    with patch.object(ctypes,'WinDLL',return_value=kernel):
                        job=control._create_windows_job(process)
                    resource=control._ProcessResources(process,{},job)
                    deadline=time.monotonic()+1;resource.deadline=deadline
                    calls=[];cleanup=resource.cleanup
                    def observe(budget):calls.append(budget);return cleanup(budget)
                    resource.cleanup=observe
                    owner=TaskCleanup()
                    def execute(*args,**kwargs):
                        self.assertFalse(resource.finish(None))
                        if path=='exception':
                            raise ProcessExecutionUncertain(cleanup_failed=True,cleanup_resource=resource)
                        return BoundedProcessResult(None,'','',cleanup_failed=True,cleanup_resource=resource)
                    with verification_owner_scope(owner):
                        with patch.object(runner,'run_bounded_process',side_effect=execute):
                            result=runner._run_verification(self.spec,self.root)
                        self.assertFalse(retry_verification_cleanup(owner));self.assertTrue(owner.has_pending)
                        with patch.object(runner,'run_bounded_process') as next_command:
                            blocked=runner._run_verification(self.spec,self.root)
                            next_command.assert_not_called()
                    trial=self.root/(failure+'-'+path);trial.mkdir()
                    with patch('tricoder.evals.runner.prepare_workspace') as prepare:
                        with verification_owner_scope(owner):
                            executor=unittest.mock.Mock()
                            next_case=runner.run_isolated_case(case,trial,executor,cleanup_owner=owner)
                        executor.assert_not_called();prepare.assert_not_called()
                    self.assertEqual(next_case.status,'error');self.assertEqual(blocked.error_code,'verification_error')
                    self.assertFalse(result.passed);self.assertTrue(owner.failed);self.assertTrue(job._handle)
                    self.assertEqual(resource.deadline,deadline);self.assertTrue(all(x<=deadline for x in calls))
                    self.assertNotIn('cleanup_resource',json.dumps(asdict(result)))
                    self.assertFalse(job._tracker.worker.is_alive())

    @unittest.skipUnless(os.name == 'nt', 'Windows native boundary')
    def test_pending_observer_blocks_next_verifier_and_case_after_result_is_dropped(self):
        import ctypes,threading
        from contextlib import nullcontext
        from types import SimpleNamespace
        from test_windows_cleanup_boundaries import Kernel,interrupt_python_join
        from tricoder.process import control
        from tricoder.evals.loader import load_suite
        case=load_suite(Path(__file__).resolve().parents[1]/'evals/quality-v3').cases[0]
        for ambient in (False,True):
            for mode in ('result','exception'):
                with self.subTest(ambient=ambient,mode=mode):
                    kernel=Kernel('missing');kernel.total=1;self.addCleanup(kernel.dispose)
                    entered=threading.Event();release=threading.Event();real_thread=threading.Thread
                    class Tail(real_thread):
                        def run(self):
                            super().run();entered.set()
                            if not release.wait(4):raise AssertionError('test observer tail expired')
                    process=SimpleNamespace(pid=10,_handle=1010,stdin=None,stdout=None,stderr=None,poll=lambda:0,wait=lambda **kw:0)
                    resource=control._ProcessResources(process,{})
                    with patch.object(ctypes,'WinDLL',return_value=kernel),patch.object(threading,'Thread',Tail):
                        job=control._create_windows_job(process,resources=resource)
                    tracker=job._tracker;kernel.messages.put((0,2,None));self.assertTrue(entered.wait(1))
                    python_alive=interrupt_python_join(tracker.worker)
                    deadline=time.monotonic()-0.01;resource.deadline=deadline
                    reference=weakref.ref(resource);holder=[resource];calls=[]
                    original_cleanup=control._ProcessResources.cleanup
                    def cleanup(budget):calls.append(budget);return original_cleanup(reference(),budget)
                    resource.cleanup=cleanup;del resource
                    parent=TaskCleanup() if ambient else None;owner=TaskCleanup();host=parent or owner
                    def execute(*args,**kwargs):
                        current=holder.pop();okay=current.finish(None)
                        if mode=='exception':raise ProcessExecutionUncertain(cleanup_failed=not okay,cleanup_resource=None if okay else current)
                        return BoundedProcessResult(0,'','',cleanup_failed=not okay,cleanup_resource=None if okay else current)
                    try:
                        with verification_owner_scope(owner),cleanup_scope(parent) if parent else nullcontext():
                            with patch.object(runner,'run_bounded_process',side_effect=execute):
                                result=runner._run_verification(self.spec,self.root)
                            self.assertFalse(result.passed);self.assertNotIn('cleanup_resource',json.dumps(asdict(result)))
                            del result;gc.collect()
                            self.assertIsNotNone(reference());self.assertTrue(host.has_pending);self.assertTrue(host.failed)
                            self.assertFalse(retry_verification_cleanup(host));self.assertFalse(tracker.complete)
                            with patch.object(runner,'run_bounded_process') as next_command:
                                blocked=runner._run_verification(self.spec,self.root);next_command.assert_not_called()
                            trial=self.root/(str(ambient)+'-'+mode);trial.mkdir()
                            with patch.object(runner,'prepare_workspace') as prepare:
                                executor=unittest.mock.Mock()
                                next_case=runner.run_isolated_case(case,trial,executor,cleanup_owner=TaskCleanup() if parent else owner)
                                prepare.assert_not_called();executor.assert_not_called()
                            self.assertEqual(next_case.status,'error');self.assertIsNotNone(next_case.timings)
                            self.assertFalse(blocked.passed)
                    finally:
                        release.set()
                        if hasattr(tracker,'observer_handle') and tracker.observer_handle.value:
                            self.assertEqual(kernel.wait(tracker.observer_handle.value,2000),0)
                        else:tracker.worker.join(1)
                        if reference() is not None:self.assertTrue(host.retry(deadline))
                    self.assertTrue(host.failed);self.assertFalse(host.has_pending)
                    self.assertTrue(calls);self.assertTrue(all(value<=deadline for value in calls))
                    self.assertFalse(job._handle)

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

    def test_failed_native_handle_close_remains_owned_for_retry(self):
        from tricoder.process import control
        from unittest.mock import Mock
        process = Mock(stdout=None, stderr=None, stdin=None)
        resource = control._ProcessResources(process, {})
        outcomes = iter((False, True))
        calls = []
        def close(handle):
            calls.append(handle)
            return next(outcomes)
        resource.native_handles.append((123, close))
        with patch.object(control, '_terminate_process_tree', return_value=True):
            self.assertFalse(resource.cleanup(7))
            self.assertEqual([(123, close)], resource.native_handles)
            self.assertTrue(resource.cleanup(7))
            self.assertEqual([], resource.native_handles)
        self.assertEqual([123,123], calls)

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
    def test_resume_failure_reaps_suspended_process_without_running_script(self):
        from tricoder.process import control
        from tricoder.policy import CommandPolicy
        (self.root/'check.py').write_text("from pathlib import Path\nPath('started').touch()\n", encoding='utf8')
        processes = []
        popen = control.subprocess.Popen
        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        policy = CommandPolicy(self.root)
        with patch.object(control.subprocess, 'Popen', side_effect=capture), \
             patch.object(control, '_resume_windows_process', side_effect=OSError('synthetic resume failure')):
            with self.assertRaises(ProcessExecutionUncertain) as caught:
                control.run_bounded_process(policy.validate('python check.py'), cwd=self.root,
                    env=policy.subprocess_environment(), timeout=5, max_output_bytes=4096)
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertIsNone(caught.exception.cleanup_resource)
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertFalse((self.root/'started').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows suspended startup cancellation')
    def test_cancellation_after_job_assignment_never_resumes_user_code(self):
        from tricoder.process import control
        from tricoder.policy import CommandPolicy
        from tricoder.core.cancellation import CancellationToken
        token = CancellationToken()
        (self.root/'check.py').write_text("from pathlib import Path\nPath('started').touch()\n", encoding='utf8')
        create = control._create_windows_job
        processes = []
        def assign(process, **kwargs):
            processes.append(process)
            job = create(process, **kwargs)
            token.cancel()
            return job
        scope = TaskCleanup()
        policy = CommandPolicy(self.root)
        with cleanup_scope(scope), patch.object(control, '_create_windows_job', side_effect=assign):
            with self.assertRaises(CancellationError) as caught:
                control.run_bounded_process(policy.validate('python check.py'), cwd=self.root,
                    env=policy.subprocess_environment(), timeout=5, max_output_bytes=4096, cancellation=token)
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertIsNotNone(scope.started_deadline)
        self.assertFalse(scope.has_pending)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertFalse((self.root/'started').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows process handle and directory evidence')
    def test_cold_start_adopts_children_before_any_user_code_runs(self):
        from tricoder.policy import CommandPolicy
        env = CommandPolicy(self.root).subprocess_environment()
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]/'src')
        completed = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).with_name('eval_cold_process_probe.py')),
             str(self.root/'cold')], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertFalse(receipt['ctypes_loaded_before_process'])
        self.assertFalse(receipt['cleanup_failed'], receipt)
        self.assertTrue(receipt['membership'], receipt)
        self.assertTrue(all(row['query_ok'] and row['in_exact_job'] for row in receipt['membership']), receipt)
        self.assertTrue(all(not row['opened'] or row['wait_before_cleanup'] == 0
                            for row in receipt['children']), receipt)
        self.assertTrue(receipt['directory_removed'])

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
