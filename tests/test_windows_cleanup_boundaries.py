import os
import queue
import threading
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from tricoder.process import control
from tricoder.core.cancellation import CancellationToken, CancellationError

E = Path(__file__).resolve().parent


class Function:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


def interrupt_python_join(worker):
    """Exercise CPython's real interruption path without rewriting private state."""
    import inspect,sys
    code=threading.Thread._wait_for_tstate_lock.__code__
    lines,start=inspect.getsourcelines(threading.Thread._wait_for_tstate_lock)
    target=start+next(i for i,line in enumerate(lines) if 'if lock.acquire(block, timeout):' in line)
    primary=KeyboardInterrupt('synthetic Python wait interruption');fired=[]
    def trace(frame,event,arg):
        if frame.f_code is code and event=='line' and frame.f_lineno==target and frame.f_locals.get('self') is worker and frame.f_locals.get('block'):
            fired.append(True);raise primary
        return trace
    try:
        sys.settrace(trace)
        try:worker.join(0.01)
        except KeyboardInterrupt as caught:assert caught is primary
        else:raise AssertionError('join injection did not fire')
    finally:sys.settrace(None)
    assert fired
    return worker.is_alive()


class Kernel:
    """Controlled native outcomes; real production tracker and observer thread."""
    def __init__(self, mode):
        self.mode=mode;self.queries=0;self.closed=[];self.waited=[];self.opened=[]
        self.unconfirmed={11};self.term_calls=0;self.allow_exit=False;self.allow_close=False
        self.total=4097 if mode=='overflow' else 3 if mode=='partial' else 2
        self.messages=queue.Queue()
        self.CreateJobObjectW=Function(lambda *a:900)
        self.SetInformationJobObject=Function(lambda *a:1)
        self.AssignProcessToJobObject=Function(lambda *a:1)
        self.CloseHandle=Function(self.close)
        self.QueryInformationJobObject=Function(self.query)
        self.OpenProcess=Function(self.open)
        self.TerminateJobObject=Function(self.terminate)
        self.WaitForSingleObject=Function(self.wait)
        self.CreateIoCompletionPort=Function(lambda *a:950)
        self.GetQueuedCompletionStatus=Function(self.receive)
        self.PostQueuedCompletionStatus=Function(lambda port,msg,key,pid:self.messages.put((msg,key,pid)) or 1)
        self.IsProcessInJob=Function(self.member)
        # The process/Job outcomes are modeled; observer identity/exit use the
        # real Windows thread object, not a fabricated successful Wait result.
        from ctypes import wintypes as w
        self.native=ctypes.WinDLL('kernel32',use_last_error=True)
        self.native.GetCurrentProcess.restype=w.HANDLE
        self.native.GetCurrentThread.restype=w.HANDLE
        self.native.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
        self.native.DuplicateHandle.restype=w.BOOL
        self.native.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];self.native.WaitForSingleObject.restype=w.DWORD
        self.native.CloseHandle.argtypes=[w.HANDLE];self.native.CloseHandle.restype=w.BOOL
        self.thread_handles={}
        self.GetCurrentProcess=Function(self.native.GetCurrentProcess)
        self.GetCurrentThread=Function(self.native.GetCurrentThread)
        self.DuplicateHandle=Function(self.duplicate)

    def duplicate(self,source,thread,target,out,rights,inherit,options):
        from ctypes import wintypes as w
        handle=w.HANDLE()
        result=self.native.DuplicateHandle(source,thread,target,ctypes.byref(handle),rights,inherit,options)
        if result:
            # Separate native thread identities from modeled process handle IDs.
            identity=500000+handle.value;self.thread_handles[identity]=handle.value;out._obj.value=identity
        return result

    def dispose(self):
        self.messages.put((0,2,None))
        for identity,handle in tuple(self.thread_handles.items()):
            assert self.native.WaitForSingleObject(handle,2000)==0
            assert self.native.CloseHandle(handle)
            del self.thread_handles[identity]

    def receive(self,port,msg,key,pid,timeout):
        values=self.messages.get(timeout=5)
        msg._obj.value,key._obj.value,pid._obj.value=values
        return 1

    def member(self,h,job,out):
        out._obj.value=self.mode!='foreign';return 1

    def query(self, handle, kind, pointer, size, returned):
        row=pointer._obj
        if kind==1:row.total=self.total;return 1
        self.queries+=1
        if self.queries==1:
            if self.mode in ('overflow','query_error'):
                row.assigned=4097 if self.mode=='overflow' else 1
                ctypes.set_last_error(234 if self.mode=='overflow' else 5);return 0
            if self.mode=='missing':row.assigned=0;row.count=0;return 1
            row.assigned=2 if self.mode=='partial' else 1;row.count=row.assigned
            row.pids[0]=11
            if self.mode=='partial':row.pids[1]=12
        else:row.assigned=0;row.count=0
        return 1

    def open(self, rights, inherit, pid):
        self.opened.append(pid)
        if self.mode=='open_error' or (self.mode=='partial' and pid==12):
            ctypes.set_last_error(5);return 0
        return pid+1000

    def terminate(self,*args):
        self.term_calls+=1
        if self.mode in ('late','dropped','duplicate') and self.term_calls==1:
            self.total=3
            if self.mode!='dropped':self.messages.put((6,1,12))
            if self.mode=='duplicate':self.messages.put((6,1,12))
        return 1

    def wait(self,handle,timeout):
        self.waited.append(handle)
        if handle in self.thread_handles:return self.native.WaitForSingleObject(self.thread_handles[handle],timeout)
        if self.mode=='wait_timeout' and handle==1011 and not self.allow_exit:return 258
        self.unconfirmed.discard(handle-1000);return 0

    def close(self,handle):
        self.closed.append(handle)
        if handle in self.thread_handles:
            result=self.native.CloseHandle(self.thread_handles[handle])
            if result:del self.thread_handles[handle]
            return result
        if self.mode=='close_failure' and handle==1011 and not self.allow_close:return 0
        return 1


@unittest.skipUnless(os.name == "nt", "Windows native boundary")
class NativeReview(unittest.TestCase):
    def job(self, model):
        self.addCleanup(model.dispose)
        with patch.object(ctypes,'WinDLL',return_value=model):
            return control._create_windows_job(SimpleNamespace(_handle=1010,pid=10))

    def record(self, *args, **kwargs):
        pass  # Receipts are the independent unittest log; no shared output files.

    def test_failed_capture_must_not_be_forgotten_on_empty_retry(self):
        for mode in ('open_error','overflow','query_error'):
            with self.subTest(mode=mode):
                m=Kernel(mode);j=self.job(m);deadline=time.monotonic()+1
                first=j.close(deadline=deadline);second=j.close(deadline=deadline)
                self.record('capture-'+mode,m,first=first,second=second,job_retained=bool(j._handle))
                self.assertFalse(first)
                self.assertFalse(second,'an empty retry list is not exit confirmation for an uncaptured process')

    def test_acquired_handle_is_retained_until_signaled_control(self):
        m=Kernel('wait_timeout');j=self.job(m);deadline=time.monotonic()+1
        self.assertFalse(j.close(deadline=deadline));self.assertNotIn(1011,m.closed);self.assertNotIn(900,m.closed)
        m.allow_exit=True
        self.assertTrue(j.close(deadline=deadline))
        self.assertGreaterEqual(m.waited.count(1011),2);self.assertEqual(m.closed.count(1011),1);self.assertEqual(m.closed.count(900),1);self.assertNotIn(1010,m.closed)
        self.record('held-handle-control',m)

    def test_cancel_during_toolhelp_must_not_resume(self):
        token=CancellationToken();events=[]
        process=SimpleNamespace(pid=7,_handle=8,stdout=None,stderr=None,stdin=None)
        def snapshot(*args): token.cancel();events.append('cancelled_in_snapshot');return 50
        def first(handle,pointer):
            pointer._obj.th32OwnerProcessID=7;pointer._obj.th32ThreadID=70;return 1
        def resume(handle):events.append('resume_after_cancel' if token.is_cancelled else 'resume');return 1
        kernel=SimpleNamespace(CreateToolhelp32Snapshot=Function(snapshot),Thread32First=Function(first),
            Thread32Next=Function(lambda *a:0),OpenThread=Function(lambda *a:51),
            ResumeThread=Function(resume),CloseHandle=Function(lambda *a:1))
        def collect(*a,**kw):token.raise_if_cancelled();self.fail('expected cancellation')
        with patch.object(control.subprocess,'Popen',return_value=process), \
             patch.object(control,'_create_windows_job',return_value=SimpleNamespace()), \
             patch.object(ctypes,'WinDLL',return_value=kernel), \
             patch.object(control,'_collect_bounded_process',side_effect=collect), \
             patch.object(control,'_terminate_process_tree',return_value=True):
            with self.assertRaises(CancellationError):
                control.run_bounded_process(['synthetic'],cwd=E,env={},timeout=1,max_output_bytes=16,cancellation=token)
        self.assertIn('cancelled_in_snapshot', events)
        self.assertNotIn('resume_after_cancel',events)

    def test_partial_handles_survive_capture_failure(self):
        m=Kernel('partial');j=self.job(m);deadline=time.monotonic()+1
        self.assertFalse(j.close(deadline=deadline));self.assertNotIn(1011,m.closed)
        self.assertFalse(j.close(deadline=deadline));self.assertIn(1011,j._tracker.members.values())
        self.assertNotIn(900,m.closed)

    def test_confirmed_handles_close_failure_stays_pending(self):
        m=Kernel('close_failure');j=self.job(m);deadline=time.monotonic()+1
        self.assertFalse(j.close(deadline=deadline));self.assertNotIn(900,m.closed)
        m.allow_close=True
        self.assertTrue(j.close(deadline=deadline));self.assertEqual(m.closed.count(900),1)

    def test_missing_member_only_clears_after_valid_evidence(self):
        m=Kernel('missing');j=self.job(m);deadline=time.monotonic()+1
        self.assertFalse(j.close(deadline=deadline));self.assertTrue(j._tracker.capture_uncertain)
        self.assertFalse(j.close(deadline=deadline))
        # An exact member handle plus a full lifetime count and signaled wait
        # supplies new evidence. A bare successful/empty query never does.
        j._tracker._capture(11)
        self.assertTrue(j.close(deadline=deadline));self.assertIn(1011,m.waited)

    def test_late_duplicate_and_missing_notifications(self):
        for mode in ('late','duplicate','dropped'):
            with self.subTest(mode=mode):
                m=Kernel(mode);j=self.job(m);confirmed=j.close(deadline=time.monotonic()+1)
                self.assertEqual(confirmed,mode!='dropped')
                if confirmed:
                    self.assertIn(1012,m.waited);self.assertEqual(m.closed.count(1012),1)
                else:
                    self.assertTrue(j._handle);self.assertEqual(j._tracker.maximum_total,3)
                    self.assertFalse(j.close(deadline=time.monotonic()+1))

    def test_foreign_identity_and_accounting_regression_stay_pending(self):
        for mode in ('foreign','missing'):
            with self.subTest(mode=mode):
                m=Kernel(mode);j=self.job(m);deadline=time.monotonic()+1
                self.assertFalse(j.close(deadline=deadline))
                m.total=1
                self.assertFalse(j.close(deadline=deadline));self.assertTrue(j._handle)

    def test_wait_error_and_port_or_job_close_failure_stay_owned(self):
        for boundary in ('wait','port_close','job_close'):
            with self.subTest(boundary=boundary):
                m=Kernel('okay');allow=[False]
                original_wait=m.WaitForSingleObject.fn;original_close=m.CloseHandle.fn
                if boundary=='wait':
                    m.WaitForSingleObject.fn=lambda h,t:0xffffffff if h==1011 and not allow[0] else original_wait(h,t)
                else:
                    failing=950 if boundary=='port_close' else 900
                    m.CloseHandle.fn=lambda h:0 if h==failing and not allow[0] else original_close(h)
                j=self.job(m);deadline=time.monotonic()+1
                self.assertFalse(j.close(deadline=deadline));self.assertTrue(j._handle)
                allow[0]=True
                self.assertTrue(j.close(deadline=deadline))
                self.assertEqual(m.closed.count(1011),1);self.assertEqual(m.closed.count(900),1)

    def test_bad_port_generation_and_failed_stop_remain_owned(self):
        for boundary in ('generation','post'):
            with self.subTest(boundary=boundary):
                m=Kernel('okay');j=self.job(m)
                if boundary=='generation':m.messages.put((6,99,11))
                else:m.PostQueuedCompletionStatus.fn=lambda *args:0
                try:
                    self.assertFalse(j.close(deadline=time.monotonic()+0.1));self.assertTrue(j._handle)
                    self.assertNotIn(900,m.closed)
                finally:
                    m.messages.put((0,2,None));j._tracker.worker.join(1)
                self.assertFalse(j._tracker.worker.is_alive())

    def test_observer_startup_failures_keep_exact_job_until_cleanup(self):
        for boundary in ('port','association','thread'):
            with self.subTest(boundary=boundary):
                m=Kernel('missing');m.total=1
                if boundary=='port':m.CreateIoCompletionPort.fn=lambda *args:0
                if boundary=='association':m.SetInformationJobObject.fn=lambda h,kind,*args:kind!=7
                with patch.object(ctypes,'WinDLL',return_value=m):
                    if boundary=='thread':
                        # Construction fails before start is attempted. An
                        # exception from start itself cannot prove non-creation.
                        with patch.object(threading,'Thread',side_effect=RuntimeError('synthetic construction')):
                            j=control._create_windows_job(SimpleNamespace(pid=10,_handle=1010))
                    else:j=control._create_windows_job(SimpleNamespace(pid=10,_handle=1010))
                self.assertIsNotNone(j);self.assertFalse(j.ready);self.assertTrue(j._handle)
                self.assertTrue(j.close(deadline=time.monotonic()+1))
                self.assertEqual(m.closed.count(900),1);self.assertNotIn(1010,m.closed)
                self.assertEqual(m.closed.count(950),0 if boundary=='port' else 1)

    def test_observer_blocked_in_native_capture_does_not_extend_deadline(self):
        m=Kernel('missing');j=self.job(m);entered=threading.Event();release=threading.Event()
        finished=threading.Event();results=[]
        original=m.OpenProcess.fn
        def blocked(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('test gate timed out')
            return original(*args)
        m.OpenProcess.fn=blocked
        m.messages.put((6,1,11));self.assertTrue(entered.wait(1))
        def cleanup():
            try:results.append(j.close(deadline=time.monotonic()+0.02))
            finally:finished.set()
        caller=threading.Thread(target=cleanup);caller.start()
        try:
            self.assertTrue(finished.wait(1),'native observer must not hold an unbounded cleanup lock')
            self.assertEqual(results,[False]);self.assertTrue(j._handle)
        finally:
            release.set();caller.join(5);j._tracker.worker.join(5)
        self.assertFalse(caller.is_alive());self.assertFalse(j._tracker.worker.is_alive())

    def test_cancel_during_open_thread_and_parent_cancellation(self):
        for point in ('snapshot','first','next','open'):
            for parent_cancel in (False,True):
                with self.subTest(point=point,parent_cancel=parent_cancel):
                    parent=CancellationToken();token=parent.create_child();target=parent if parent_cancel else token
                    process=SimpleNamespace(pid=7,stdout=None,stderr=None,stdin=None)
                    resources=control._ProcessResources(process,{})
                    events=[]
                    def snapshot(*args):
                        if point=='snapshot':target.cancel()
                        return 50
                    def first(h,ptr):
                        if point=='first':target.cancel()
                        ptr._obj.th32OwnerProcessID=8 if point=='next' else 7
                        ptr._obj.th32ThreadID=70;return 1
                    def next_thread(h,ptr):
                        target.cancel();ptr._obj.th32OwnerProcessID=7;return 1
                    def open_thread(*args):
                        if point=='open':target.cancel()
                        return 51
                    kernel=SimpleNamespace(CreateToolhelp32Snapshot=Function(snapshot),Thread32First=Function(first),
                        Thread32Next=Function(next_thread),OpenThread=Function(open_thread),
                        ResumeThread=Function(lambda h:events.append('resume') or 1),CloseHandle=Function(lambda h:1))
                    with patch.object(ctypes,'WinDLL',return_value=kernel):
                        with self.assertRaises(CancellationError):
                            control._resume_windows_process(process,resources,cancellation=token)
                    self.assertEqual(events,[])
                    self.assertIn(50,[h for h,_ in resources.native_handles])
                    if point=='open':self.assertIn(51,[h for h,_ in resources.native_handles])
                    with patch.object(control,'_terminate_process_tree',return_value=True):
                        self.assertTrue(resources.cleanup(time.monotonic()+1))
                    self.assertEqual(resources.native_handles,[])

    def test_resume_failure_and_unexpected_suspend_count_keep_handles(self):
        for result in (0xffffffff,0,2):
            with self.subTest(result=result):
                process=SimpleNamespace(pid=7,stdout=None,stderr=None,stdin=None)
                resources=control._ProcessResources(process,{})
                def first(h,p):p._obj.th32OwnerProcessID=7;p._obj.th32ThreadID=70;return 1
                kernel=SimpleNamespace(CreateToolhelp32Snapshot=Function(lambda *a:50),Thread32First=Function(first),
                    Thread32Next=Function(lambda *a:0),OpenThread=Function(lambda *a:51),
                    ResumeThread=Function(lambda *a:result),CloseHandle=Function(lambda *a:0))
                with patch.object(ctypes,'WinDLL',return_value=kernel):
                    with self.assertRaises(OSError):control._resume_windows_process(process,resources)
                with patch.object(control,'_terminate_process_tree',return_value=True):
                    self.assertFalse(resources.cleanup(time.monotonic()+1))
                self.assertEqual([h for h,_ in resources.native_handles],[50,51])


@unittest.skipUnless(os.name == 'nt','Windows native boundary')
class RealWindowsBoundaries(unittest.TestCase):
    def run_probe(self, mode, *args):
        import subprocess,sys,uuid
        base=Path(os.environ.get('TRICODER_V3_EVIDENCE',str(Path.cwd()/'runtime'/('wcb-'+uuid.uuid4().hex))))
        base.mkdir(parents=True,exist_ok=True)
        destination=base/(mode+'-'+uuid.uuid4().hex[:8])
        completed=subprocess.run([sys.executable,'-X','utf8','-B',str(Path(__file__).with_name('windows_cleanup_boundary_probe.py')),
            mode,str(destination),*args],capture_output=True,text=True,encoding='utf8',timeout=45)
        (base/(destination.name+'.log')).write_text(completed.stdout+completed.stderr,encoding='utf8')
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        return destination

    def test_late_member_before_closure_is_awaited_ten_real_iterations(self):
        destination=self.run_probe('late')
        rows=json.loads((destination/'real-late-threaded-member-v3.json').read_text())
        self.assertEqual(len(rows),10)
        self.assertTrue(all(r['cleanup_confirmed'] and r['late_child_in_exact_job']
            and r['late_child_opened_by_implementation'] and r['late_child_wait_at_return']==0 for r in rows))

    def test_creation_at_termination_boundary_is_accounted_ten_iterations(self):
        destination=self.run_probe('late','terminate')
        rows=json.loads((destination/'real-late-threaded-member-v3.json').read_text())
        self.assertEqual(len(rows),10)
        self.assertTrue(all(r['cleanup_confirmed'] and r['late_child_wait_at_return']==0 for r in rows))
        self.assertTrue(all(r['trigger_stage']=='terminate' for r in rows), 'probe must reach actual termination boundary')

    def test_normal_quick_and_grandchild_processes_still_succeed(self):
        import tempfile,sys
        from tricoder.policy import CommandPolicy
        scripts={
            'plain':'pass',
            'quick':"import subprocess,sys;subprocess.run([sys.executable,'-c','pass'],check=True)",
            'grandchild':"import subprocess,sys;subprocess.run([sys.executable,'-c',\"import subprocess,sys;subprocess.run([sys.executable,'-c','pass'],check=True)\"],check=True)",
        }
        for kind,script in scripts.items():
            for attempt in range(3):
                with self.subTest(kind=kind,attempt=attempt),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory).resolve()
                    result=control.run_bounded_process([sys._base_executable,'-c',script],cwd=root,
                        env=CommandPolicy(root).subprocess_environment(),timeout=5,max_output_bytes=4096)
                    self.assertEqual(result.returncode,0);self.assertFalse(result.cleanup_failed)
                    self.assertIsNone(result.cleanup_resource)

    def test_primary_interrupt_during_observer_start_keeps_native_owner(self):
        import tempfile,sys
        from tricoder.policy import CommandPolicy
        from tricoder.process.windows_job import WindowsJobTracker
        primary=KeyboardInterrupt('synthetic observer start')
        trackers=[];original=WindowsJobTracker.start
        def interrupted(tracker,**kwargs):
            trackers.append(tracker);original(tracker,**kwargs);raise primary
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();marker=root/'executed'
            try:
                with patch.object(WindowsJobTracker,'start',side_effect=interrupted,autospec=True):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        control.run_bounded_process([sys._base_executable,'-c',"from pathlib import Path;Path('executed').touch()"],
                            cwd=root,env=CommandPolicy(root).subprocess_environment(),timeout=5,max_output_bytes=4096)
                self.assertIs(caught.exception,primary);self.assertFalse(marker.exists())
                self.assertEqual(len(trackers),1)
                self.assertFalse(trackers[0].worker.is_alive(),'observer lost at factory exception boundary')
                self.assertEqual(trackers[0].job._handle,0)
            finally:
                for tracker in trackers:
                    if tracker.job._handle:
                        self.assertTrue(tracker.job.close(deadline=time.monotonic()+5))

    def test_actual_cancelled_script_never_signals_snapshot_open_and_parent(self):
        for point in ('snapshot','open'):
            for parent in ('child','parent'):
                with self.subTest(point=point,parent=parent):
                    destination=self.run_probe('cancel',point,parent)
                    rows=json.loads((destination/'real-cancel-during-snapshot.json').read_text())
                    self.assertTrue(any(r.get('cancelled') for r in rows))
                    self.assertFalse(any(r.get('stage')=='resume' for r in rows))
                    self.assertFalse(rows[-1]['cleanup_failed'])


@unittest.skipUnless(os.name == 'nt','Windows native boundary')
class ObserverLifecycleRegression(unittest.TestCase):
    def probe(self, mode):
        import subprocess,sys,uuid
        base=Path(os.environ.get('TRICODER_V3_EVIDENCE',str(Path.cwd()/'runtime'/('r4-'+uuid.uuid4().hex))))
        base.mkdir(parents=True,exist_ok=True)
        destination=base/('observer-'+mode+'-'+uuid.uuid4().hex[:8])
        env=dict(os.environ);env['TRICODER_V3_EVIDENCE']=str(destination)
        result=subprocess.run([sys.executable,'-X','utf8','-B',str(Path(__file__).with_name('windows_observer_lifecycle_probe.py')),mode],
            env=env,capture_output=True,text=True,encoding='utf8',timeout=35)
        (base/(destination.name+'.log')).write_text(result.stdout+result.stderr,encoding='utf8')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_start_attempt_interrupted_before_python_ack_stays_pending(self):
        self.probe('startup')

    def test_public_interrupt_keeps_owner_until_late_capture_and_exit(self):
        self.probe('public')

    def test_second_interrupt_keeps_primary_and_consumed_deadline(self):
        self.probe('second')

    def test_real_finish_wait_interrupt_remains_pending_until_native_exit(self):
        self.probe('finish')

    def test_real_ready_cancellation_keeps_root_suspended_for_own_and_parent(self):
        for mode in ('ready-child','ready-parent'):
            with self.subTest(mode=mode):self.probe(mode)


@unittest.skipUnless(os.name == 'nt','Windows native boundary')
class ObserverNativeBoundaries(unittest.TestCase):
    def model(self):
        kernel=Kernel('missing');kernel.total=1
        self.addCleanup(kernel.dispose)
        return kernel

    def job(self,kernel,**kwargs):
        with patch.object(ctypes,'WinDLL',return_value=kernel):
            return control._create_windows_job(SimpleNamespace(pid=10,_handle=1010),**kwargs)

    def test_start_exception_is_unknown_even_when_python_ident_is_empty(self):
        kernel=self.model()
        with patch.object(threading.Thread,'start',side_effect=RuntimeError('synthetic unknown start')):
            job=self.job(kernel)
        self.assertFalse(job.ready);self.assertTrue(job._tracker.start_attempted)
        self.assertIsNone(job._tracker.worker.ident)
        self.assertFalse(job.close(deadline=time.monotonic()))
        self.assertTrue(job._handle);self.assertTrue(job._tracker.port)
        self.assertFalse(job._tracker.complete)

    def test_observer_done_before_native_exit_is_not_completion(self):
        kernel=self.model();entered=threading.Event();release=threading.Event()
        real_thread=threading.Thread
        class Tail(real_thread):
            def run(self):
                super().run();entered.set()
                if not release.wait(3):raise AssertionError('test tail gate expired')
        with patch.object(threading,'Thread',Tail):job=self.job(kernel)
        tracker=job._tracker;deadline=time.monotonic()+0.02
        try:
            self.assertFalse(job.close(deadline=deadline))
            self.assertTrue(entered.wait(1));self.assertTrue(tracker.observer_done.is_set())
            self.assertFalse(tracker.complete);self.assertTrue(tracker.port);self.assertTrue(job._handle)
            self.assertEqual(kernel.wait(tracker.observer_handle.value,0),258)
        finally:release.set()
        self.assertEqual(kernel.wait(tracker.observer_handle.value,2000),0)
        self.assertTrue(job.close(deadline=deadline)) # expired, only zero-time proof
        self.assertFalse(tracker.observer_handle.value);self.assertIsNone(tracker.port)

    def test_native_wait_failed_timeout_close_and_post_failure_keep_owner(self):
        for fault in ('wait_failed','wait_timeout','close','post'):
            with self.subTest(fault=fault):
                kernel=self.model();job=self.job(kernel);tracker=job._tracker
                thread=tracker.observer_handle.value;allow=[False];deadline=time.monotonic()+1
                wait=kernel.WaitForSingleObject.fn;close=kernel.CloseHandle.fn;post=kernel.PostQueuedCompletionStatus.fn
                if fault.startswith('wait'):
                    kernel.WaitForSingleObject.fn=lambda h,t: (258 if fault=='wait_timeout' else 0xffffffff) if h==thread and not allow[0] else wait(h,t)
                elif fault=='close':kernel.CloseHandle.fn=lambda h:0 if h==thread and not allow[0] else close(h)
                else:kernel.PostQueuedCompletionStatus.fn=lambda *a:0 if not allow[0] else post(*a)
                self.assertFalse(job.close(deadline=deadline));self.assertTrue(job._handle)
                self.assertEqual(tracker.observer_handle.value,thread)
                allow[0]=True
                self.assertTrue(job.close(deadline=deadline))
                self.assertEqual(kernel.closed.count(thread),1)

    def test_identity_failure_done_does_not_prove_native_exit(self):
        kernel=self.model();kernel.DuplicateHandle.fn=lambda *a:0
        job=self.job(kernel);tracker=job._tracker
        self.assertFalse(job.ready);self.assertTrue(tracker.observer_done.wait(1))
        self.assertFalse(job.close(deadline=time.monotonic()))
        self.assertTrue(job._handle);self.assertTrue(tracker.port);self.assertFalse(tracker.complete)
        # Test-owned Python thread disposal is not used as a product certificate.
        tracker.worker.join(1)

    def test_interrupted_python_thread_state_cannot_certify_observer_exit(self):
        kernel=self.model();entered=threading.Event();release=threading.Event()
        original=kernel.OpenProcess.fn
        def blocked(*args):
            handle=original(*args);entered.set()
            if not release.wait(3):raise AssertionError('capture gate expired')
            return handle
        kernel.OpenProcess.fn=blocked;job=self.job(kernel);tracker=job._tracker
        kernel.total=2;kernel.messages.put((6,1,11));self.assertTrue(entered.wait(1))
        try:
            interrupt_python_join(tracker.worker)
            self.assertFalse(job.close(deadline=time.monotonic()+0.02))
            self.assertTrue(tracker.stop_sent);self.assertFalse(tracker.complete)
            self.assertTrue(job._handle);self.assertTrue(tracker.port)
        finally:release.set()
        self.assertEqual(kernel.wait(tracker.observer_handle.value,2000),0)
        self.assertTrue(job.close(deadline=time.monotonic()))

    def test_cancel_during_observer_ready_preserves_own_and_parent_mapping(self):
        from tricoder.process.windows_job import WindowsJobTracker
        for parent_cancel in (False,True):
            with self.subTest(parent=parent_cancel):
                kernel=self.model();parent=CancellationToken();token=parent.create_child()
                target=parent if parent_cancel else token;release=threading.Event();entered=threading.Event();waited=[]
                duplicate=kernel.DuplicateHandle.fn;start=WindowsJobTracker.start
                def blocked(*args):
                    result=duplicate(*args);entered.set()
                    if not release.wait(2):raise AssertionError('ready gate expired')
                    return result
                kernel.DuplicateHandle.fn=blocked
                def observe(tracker,**kwargs):
                    original=tracker.observer_ready.wait
                    def ready_wait(timeout):
                        if waited:return original(timeout)
                        self.assertTrue(entered.wait(1));waited.append(timeout);target.cancel();release.set()
                        return original(0)
                    tracker.observer_ready.wait=ready_wait
                    return start(tracker,**kwargs)
                process=SimpleNamespace(pid=10,_handle=1010,stdin=None,stdout=None,stderr=None,
                    poll=lambda:0,wait=lambda **kw:0)
                resources=control._ProcessResources(process,{})
                try:
                    with patch.object(WindowsJobTracker,'start',observe),patch.object(ctypes,'WinDLL',return_value=kernel):
                        with self.assertRaises(CancellationError):
                            control._create_windows_job(process,resources=resources,deadline=time.monotonic()+1,cancellation=token)
                    self.assertTrue(waited);self.assertIsNotNone(resources.windows_job)
                finally:release.set()
                self.assertTrue(resources.finish(token));self.assertEqual(resources.windows_job._handle,0)

    def test_ready_budget_does_not_issue_a_fresh_execution_deadline(self):
        kernel=self.model();release=threading.Event();original=kernel.DuplicateHandle.fn
        def delayed(*args):
            result=original(*args)
            if not release.wait(2):raise AssertionError('ready gate expired')
            return result
        kernel.DuplicateHandle.fn=delayed
        start=time.monotonic();job=self.job(kernel,deadline=start+0.02)
        try:
            self.assertFalse(job.ready);self.assertLess(time.monotonic()-start,0.5)
            self.assertFalse(job.close(deadline=time.monotonic()))
        finally:release.set()
        self.assertTrue(job._tracker.observer_ready.wait(1))
        self.assertEqual(kernel.wait(job._tracker.observer_handle.value,2000),0)
        self.assertTrue(job.close(deadline=start+0.02))


if __name__=='__main__':unittest.main(verbosity=2)
