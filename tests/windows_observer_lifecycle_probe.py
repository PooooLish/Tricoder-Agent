"""Reviewer fault injection: native observer created, Python startup acknowledgement interrupted."""
import ctypes,json,os,sys,tempfile,threading,time,unittest,inspect,queue,subprocess
from ctypes import wintypes as w
from pathlib import Path
from unittest.mock import patch
from tricoder.process import control
from tricoder.process.windows_job import WindowsJobTracker
from tricoder.policy import CommandPolicy
from tricoder.task_cleanup import TaskCleanup,cleanup_scope

OUT=Path(os.environ['TRICODER_V3_EVIDENCE']);OUT.mkdir(parents=True,exist_ok=False)
MODE=sys.argv[1]
class StartupBoundary(unittest.TestCase):
 def test_native_thread_started_but_not_acknowledged_must_remain_owned(self):
  entered=threading.Event();release=threading.Event();exited=threading.Event()
  primary=KeyboardInterrupt('synthetic interruption of Thread.start acknowledgement')
  real_thread=threading.Thread;workers=[];trackers=[];resources=[];scope=TaskCleanup()
  class DeferredBootstrap(real_thread):
   def __init__(self,*args,**kwargs):
    super().__init__(*args,**kwargs);workers.append(self)
   def _bootstrap_inner(self):
    entered.set()
    try:
     if not release.wait(15):raise AssertionError('reviewer bootstrap gate expired')
     super()._bootstrap_inner()
    finally:exited.set()
  factory=control._create_windows_job
  def create(process,**kwargs):resources.append(kwargs['resources']);return factory(process,**kwargs)
  original_start=WindowsJobTracker.start
  def observe(tracker,**kwargs):trackers.append(tracker);return original_start(tracker,**kwargs)
  facts={};injected=[]
  code=threading.Thread.start.__code__
  lines,start=inspect.getsourcelines(threading.Thread.start)
  target=start+next(i for i,line in enumerate(lines) if 'self._started.wait()' in line)
  def trace(frame,event,arg):
   if frame.f_code is code and event=='line' and frame.f_lineno==target:
    if not entered.wait(3):raise AssertionError('native observer not launched')
    injected.append('start_ack');raise primary
   return trace
  try:
   with tempfile.TemporaryDirectory(dir=OUT) as d:
    root=Path(d).resolve();policy=CommandPolicy(root)
    with patch.object(threading,'Thread',DeferredBootstrap),patch.object(WindowsJobTracker,'start',observe),patch.object(control,'_create_windows_job',create),cleanup_scope(scope):
     sys.settrace(trace)
     with self.assertRaises(KeyboardInterrupt) as caught:
      control.run_bounded_process([sys._base_executable,'-c',"from pathlib import Path;Path('executed').touch()"],cwd=root,env=policy.subprocess_environment(),timeout=2,max_output_bytes=4096)
    sys.settrace(None)
    t=trackers[0]
    facts=dict(primary_preserved=caught.exception is primary,native_observer_entered=entered.is_set(),native_observer_exited=exited.is_set(),worker_ident=t.worker.ident,job_handle=t.job._handle,port=t.port,complete=t.complete,scope_pending=scope.has_pending,scope_failed=scope.failed,exception_cleanup_failed=getattr(caught.exception,'cleanup_failed',False),root_exited=t.process.poll() is not None,script_executed=(root/'executed').exists())
  finally:
   sys.settrace(None);release.set()
   if workers:
    if not exited.wait(5):raise AssertionError('test-owned observer cleanup failed')
    workers[0].join(5)
   for t in trackers:
    if hasattr(t,'observer_handle') and t.observer_handle.value:
     self.assertEqual(t.k.WaitForSingleObject(t.observer_handle.value,2000),0)
    if t.job._handle:self.assertTrue(resources[0].cleanup(resources[0].deadline))
   if resources:
    facts['retry_original_deadline']=resources[0].deadline
    facts['retry_after_deadline_expired']=time.monotonic()>=resources[0].deadline
    self.assertTrue(scope.retry(resources[0].deadline));facts['failed_after_cleanup']=scope.failed
   facts['observer_exited_after_test_release']=exited.is_set();facts['injections']=injected
   (OUT/'startup-interruption.json').write_text(json.dumps(facts,indent=2),encoding='utf8')
  self.assertTrue(facts['native_observer_entered']);self.assertFalse(facts['native_observer_exited'])
  self.assertTrue(facts['scope_pending'],'cleanup must retain a native observer whose bootstrap is still pending')
  self.assertNotEqual(facts['job_handle'],0,'do not close the observer resources before startup/exit confirmation')


class Fn:
 def __init__(self,fn):self.fn=fn
 def __call__(self,*args):return self.fn(*args)
class RealPublicBoundary(unittest.TestCase):
 def test_primary_interrupt_does_not_drop_running_observer(self):
  k=ctypes.WinDLL('kernel32',use_last_error=True)
  k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
  k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
  k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.WaitForSingleObject.restype=w.DWORD
  main=threading.get_ident();entered=threading.Event();release=threading.Event();exited=threading.Event()
  trackers=[];resources=[];facts={};scope=TaskCleanup();primary=KeyboardInterrupt('synthetic interrupt at observer wait');secondary=KeyboardInterrupt('synthetic secondary cleanup interrupt');injected=[];opened=[];closed=[];wait_deadlines=[]
  class Proxy:
   def __getattr__(self,name):return getattr(k,name)
  proxy=Proxy()
  def acquire(rights,inherit,pid):
   h=k.OpenProcess(rights,inherit,pid)
   if threading.get_ident()!=main and h:
    opened.append(h);entered.set()
    if not release.wait(12):raise AssertionError('review gate expired')
   return h
  proxy.OpenProcess=Fn(acquire)
  def close(handle):
   result=k.CloseHandle(handle)
   if result:closed.append(handle)
   return result
  proxy.CloseHandle=Fn(close)
  original_monitor=WindowsJobTracker._monitor
  def monitored(t):
   try:original_monitor(t)
   finally:exited.set()
  factory=control._create_windows_job
  def create(process,**kwargs):
   resources.append(kwargs['resources']);job=factory(process,**kwargs);trackers.append(job._tracker);return job
  finish=control._ProcessResources.finish
  def finish_after_native_gate(resource,*args,**kwargs):
   if not entered.wait(3):raise AssertionError('observer did not acquire a child handle')
   if resource.deadline is None:resource.deadline=time.monotonic()+0.15
   return finish(resource,*args,**kwargs)
  code=threading.Thread._wait_for_tstate_lock.__code__
  lines,start=inspect.getsourcelines(threading.Thread._wait_for_tstate_lock)
  line=start+next(i for i,s in enumerate(lines) if 'if lock.acquire(block, timeout):' in s)
  def trace(frame,event,arg):
   if trackers and frame.f_code is code and event=='line' and frame.f_lineno==line and frame.f_locals.get('block') is True and frame.f_locals.get('self') is trackers[0].worker:
    injected.append('python_join');raise primary
   return trace
  original_wait=WindowsJobTracker._wait
  def wait(tracker,handle,deadline):
   wait_deadlines.append(deadline)
   limit=2 if MODE=='second' else 1
   if hasattr(tracker,'observer_handle') and handle==tracker.observer_handle.value and len(injected)<limit:
    self.assertEqual(k.WaitForSingleObject(handle,0),258)
    injected.append('native_observer_wait_timeout_then_interrupt');raise primary if len(injected)==1 else secondary
   return original_wait(tracker,handle,deadline)
  try:
   with tempfile.TemporaryDirectory(dir=OUT) as directory:
    root=Path(directory).resolve();env=CommandPolicy(root).subprocess_environment()
    script="import subprocess,sys,threading;subprocess.Popen([sys.executable,'-c','import threading;threading.Event().wait()'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);threading.Event().wait()"
    with patch.object(ctypes,'WinDLL',return_value=proxy),patch.object(WindowsJobTracker,'_monitor',monitored),patch.object(control,'_create_windows_job',create),patch.object(control._ProcessResources,'finish',finish_after_native_gate),patch.object(WindowsJobTracker,'_wait',wait),cleanup_scope(scope):
     try:
      sys.settrace(trace)
      with self.assertRaises(KeyboardInterrupt) as caught:
       control.run_bounded_process([sys._base_executable,'-c',script],cwd=root,env=env,timeout=0.3,max_output_bytes=4096)
     finally:sys.settrace(None)
    t=trackers[0];resource=resources[0]
    facts=dict(primary_preserved=caught.exception is primary,exception_cleanup_failed=getattr(caught.exception,'cleanup_failed',False),exception_has_resource=getattr(caught.exception,'cleanup_resource',None) is resource,scope_pending=scope.has_pending,scope_failed=scope.failed,observer_exited=exited.is_set(),python_is_alive=t.worker.is_alive(),job_handle=t.job._handle,port=t.port,complete=t.complete,root_exited=resource.process.poll() is not None)
    release.set();self.assertTrue(exited.wait(3))
    thread_handle=t.observer_handle.value if hasattr(t,'observer_handle') else None
    if thread_handle:self.assertEqual(k.WaitForSingleObject(thread_handle,2000),0)
    facts['injections']=injected
    facts['extra_handle_count_after_observer_exit']=len(t.extras)
    facts['extra_handles_valid']=[k.WaitForSingleObject(h,0)==0 for h in t.extras]
    self.assertTrue(resource.cleanup(resource.deadline))
    facts['extra_handle_count_after_resource_retry']=len(t.extras)
    facts['observer_acquired_handles_closed_once']=all(closed.count(h)==1 for h in opened)
    facts['observer_thread_closed_once']=thread_handle is not None and closed.count(thread_handle)==1
    facts['original_deadline_preserved']=all(d<=resource.deadline for d in wait_deadlines)
    self.assertTrue(scope.retry(resource.deadline));facts['failed_after_cleanup']=scope.failed
    self.assertTrue(facts['observer_acquired_handles_closed_once']);self.assertTrue(facts['observer_thread_closed_once'])
    self.assertTrue(facts['original_deadline_preserved']);self.assertTrue(scope.failed)
    self.assertEqual(len(injected),2 if MODE=='second' else 1)
  finally:
   sys.settrace(None);release.set()
   for t in trackers:
    if not exited.wait(3):
     if t.port:t.k.PostQueuedCompletionStatus(t.port,0,2,None)
     self.assertTrue(exited.wait(3))
    self.assertTrue(t._close_owned())
   for resource in resources:self.assertTrue(resource.cleanup(time.monotonic()+3))
   facts['test_owned_cleanup_complete']=True
   (OUT/'public-join-interruption.json').write_text(json.dumps(facts,indent=2),encoding='utf8')
  self.assertTrue(facts['scope_pending'],'public execution boundary must retain still-running observer after primary interrupt')
  self.assertTrue(facts['exception_cleanup_failed'])
  self.assertTrue(facts['exception_has_resource']);self.assertFalse(facts['observer_exited'])
  self.assertTrue(facts['job_handle']);self.assertTrue(facts['port']);self.assertFalse(facts['complete'])
  self.assertEqual(facts['extra_handle_count_after_resource_retry'],0);self.assertTrue(injected)



class RealJoinBoundary(unittest.TestCase):
 def test_join_interrupt_must_not_release_live_native_observer(self):
  k=ctypes.WinDLL('kernel32',use_last_error=True)
  k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
  k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
  k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.WaitForSingleObject.restype=w.DWORD
  main=threading.get_ident();entered=threading.Event();release=threading.Event();exited=threading.Event()
  opened_by_observer=[];facts={};scope=TaskCleanup();primary=KeyboardInterrupt('synthetic join-boundary interrupt')
  class Proxy:
   def __getattr__(self,name):return getattr(k,name)
  proxy=Proxy()
  def acquire(rights,inherit,pid):
   h=k.OpenProcess(rights,inherit,pid)
   if threading.get_ident()!=main and h:
    opened_by_observer.append(h);entered.set()
    if not release.wait(10):raise AssertionError('review gate expired')
   return h
  proxy.OpenProcess=Fn(acquire)
  original_monitor=WindowsJobTracker._monitor
  def monitored(t):
   try:original_monitor(t)
   finally:exited.set()
  code=threading.Thread._wait_for_tstate_lock.__code__
  lines,start=inspect.getsourcelines(threading.Thread._wait_for_tstate_lock)
  target=start+next(i for i,line in enumerate(lines) if 'if lock.acquire(block, timeout):' in line)
  injections=[]
  def trace(frame,event,arg):
   if frame.f_code is code and event=='line' and frame.f_lineno==target and frame.f_locals.get('block') is True:
    injections.append('Thread.join/_wait_for_tstate_lock');raise primary
   return trace
  original_wait=WindowsJobTracker._wait
  def native_wait(owner,handle,deadline):
   if hasattr(owner,'observer_handle') and handle==owner.observer_handle.value and not injections:
    self.assertEqual(k.WaitForSingleObject(handle,0),258)
    injections.append('native_observer_wait_timeout_then_interrupt');raise primary
   return original_wait(owner,handle,deadline)
  with tempfile.TemporaryDirectory(dir=OUT) as directory:
   root=Path(directory).resolve();env=CommandPolicy(root).subprocess_environment()
   script="import subprocess,sys,threading;p=subprocess.Popen([sys.executable,'-c','import threading;threading.Event().wait()'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid,flush=True);threading.Event().wait()"
   p=subprocess.Popen([sys._base_executable,'-u','-c',script],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x4|subprocess.CREATE_NEW_PROCESS_GROUP)
   resource=control._ProcessResources(p,env);reader=None;held=None;tracker=None
   try:
    with patch.object(ctypes,'WinDLL',return_value=proxy),patch.object(WindowsJobTracker,'_monitor',monitored):job=control._create_windows_job(p,resources=resource)
    self.assertTrue(job.ready);tracker=job._tracker
    control._resume_windows_process(p,resource)
    messages=queue.Queue()
    def read():
     for line in iter(p.stdout.readline,b''):messages.put(line.strip())
    reader=threading.Thread(target=read,daemon=True);reader.start();resource.readers=(reader,)
    child=int(messages.get(timeout=4));self.assertTrue(entered.wait(4))
    held=k.OpenProcess(0x00101000,False,child);self.assertTrue(held)
    resource.deadline=time.monotonic()+0.15
    with cleanup_scope(scope),patch.object(WindowsJobTracker,'_wait',native_wait):
     try:
      sys.settrace(trace)
      with self.assertRaises(KeyboardInterrupt) as caught:resource.finish(None)
     finally:sys.settrace(None)
     confirmed=resource.finish(None)
    facts=dict(injection=injections,primary_preserved=caught.exception is primary,retry_confirmed=confirmed,observer_exited=exited.is_set(),python_is_alive=tracker.worker.is_alive(),scope_pending=scope.has_pending,scope_failed=scope.failed,job_handle=job._handle,port=tracker.port,complete=tracker.complete,stop_sent=tracker.stop_sent,child_wait_at_return=k.WaitForSingleObject(held,0))
    release.set();self.assertTrue(exited.wait(3))
    if hasattr(tracker,'observer_handle') and tracker.observer_handle.value:self.assertEqual(k.WaitForSingleObject(tracker.observer_handle.value,2000),0)
    facts['extra_handle_count_after_observer_exit']=len(tracker.extras)
    facts['extra_handles_still_valid']=[k.WaitForSingleObject(h,0)==0 for h in tracker.extras]
    self.assertTrue(resource.cleanup(resource.deadline))
    facts['extra_handle_count_after_resource_retry']=len(tracker.extras)
   finally:
    sys.settrace(None);release.set()
    if tracker is not None:
     if not exited.wait(3):
      if tracker.port:tracker.k.PostQueuedCompletionStatus(tracker.port,0,2,None)
      self.assertTrue(exited.wait(3))
     self.assertTrue(tracker._close_owned())
    self.assertTrue(resource.cleanup(time.monotonic()+3))
    if held:self.assertTrue(k.CloseHandle(held))
    if reader:reader.join(3);self.assertFalse(reader.is_alive())
    facts['test_owned_cleanup_complete']=True
    (OUT/'real-join-interruption.json').write_text(json.dumps(facts,indent=2),encoding='utf8')
  self.assertFalse(facts['retry_confirmed'],'live observer must remain pending despite interrupted Python join state')
  self.assertTrue(injections);self.assertTrue(facts['scope_pending']);self.assertTrue(facts['job_handle'])
  self.assertFalse(facts['complete']);self.assertEqual(facts['extra_handle_count_after_resource_retry'],0)

class RealReadyCancellation(unittest.TestCase):
 def test_ready_cancellation_does_not_resume_root_or_change_error_mapping(self):
  from tricoder.core.cancellation import CancellationToken,CancellationError
  k=ctypes.WinDLL('kernel32',use_last_error=True)
  k.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
  k.DuplicateHandle.restype=w.BOOL
  release=threading.Event();entered=threading.Event();waited=[];resources=[];resumed=[]
  parent=CancellationToken();token=parent.create_child();target=parent if MODE=='ready-parent' else token
  scope=TaskCleanup();factory=control._create_windows_job;start=WindowsJobTracker.start
  class Proxy:
   def __getattr__(self,name):return getattr(k,name)
  proxy=Proxy()
  def duplicate(*args):
   result=k.DuplicateHandle(*args);entered.set()
   if not release.wait(5):raise AssertionError('identity return gate expired')
   return result
  proxy.DuplicateHandle=Fn(duplicate)
  def create(process,**kwargs):resources.append(kwargs['resources']);return factory(process,**kwargs)
  def observe(tracker,**kwargs):
   original=tracker.observer_ready.wait
   def wait(timeout):
    if waited:return original(timeout)
    self.assertTrue(entered.wait(2));self.assertIsNone(scope.started_deadline)
    waited.append(timeout);target.cancel();release.set();return original(0)
   tracker.observer_ready.wait=wait
   return start(tracker,**kwargs)
  resume=control._resume_windows_process
  def resume_root(*args,**kwargs):resumed.append(True);return resume(*args,**kwargs)
  facts={}
  try:
   with tempfile.TemporaryDirectory(dir=OUT) as directory:
    root=Path(directory).resolve();env=CommandPolicy(root).subprocess_environment()
    with patch.object(ctypes,'WinDLL',return_value=proxy),patch.object(control,'_create_windows_job',create),patch.object(WindowsJobTracker,'start',observe),patch.object(control,'_resume_windows_process',resume_root),cleanup_scope(scope):
     with self.assertRaises(CancellationError) as caught:
      control.run_bounded_process([sys._base_executable,'-c',"from pathlib import Path;Path('executed').touch()"],cwd=root,env=env,timeout=2,max_output_bytes=4096,cancellation=token)
    resource=resources[0];tracker=resource.windows_job._tracker
    facts=dict(parent=MODE=='ready-parent',injected_ready_wait=bool(waited),resume_calls=len(resumed),script_executed=(root/'executed').exists(),
      cancelled=token.is_cancelled,cleanup_failed=caught.exception.cleanup_failed,job_closed=not resource.windows_job._handle,
      port_closed=tracker.port is None,observer_closed=not tracker.observer_handle.value,observer_exited=tracker.observer_exited,
      scope_pending=scope.has_pending,scope_failed=scope.failed)
    self.assertTrue(waited);self.assertFalse(resumed);self.assertFalse(facts['script_executed'])
    self.assertFalse(caught.exception.cleanup_failed);self.assertTrue(facts['job_closed']);self.assertTrue(facts['observer_closed'])
    self.assertFalse(scope.has_pending)
  finally:
   release.set()
   for resource in resources:
    if resource.windows_job._handle:self.assertTrue(resource.cleanup(resource.deadline))
   (OUT/'ready-cancellation.json').write_text(json.dumps(facts,indent=2),encoding='utf8')

if __name__=='__main__':
 name=sys.argv.pop(1)
 case=StartupBoundary if name=='startup' else RealJoinBoundary if name=='finish' else RealReadyCancellation if name.startswith('ready-') else RealPublicBoundary
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().loadTestsFromTestCase(case))
 sys.exit(not result.wasSuccessful())
