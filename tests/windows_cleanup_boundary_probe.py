"""Real isolated Windows boundary probes; no simulated wait/termination results."""
import ctypes,json,queue,subprocess,sys,tempfile,threading,time,uuid,shutil
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch
from tricoder.process import control
from tricoder.policy import CommandPolicy
from tricoder.core.cancellation import CancellationToken,CancellationError

def late_members(E, attempts=10, boundary="query"):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
    k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
    k.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];k.TerminateProcess.restype=wintypes.BOOL
    k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)];k.IsProcessInJob.restype=wintypes.BOOL

    class Fn:
        def __init__(self,fn):self.fn=fn
        def __call__(self,*a):return self.fn(*a)

    child="import threading;e=threading.Event();[threading.Thread(target=e.wait,daemon=True).start() for _ in range(64)];print('ready',flush=True);e.wait()"
    script=("import sys,subprocess,threading\nprint('ready',flush=True)\n"
            "assert sys.stdin.readline().strip()=='spawn'\n"
            f"p=subprocess.Popen([sys.executable,'-u','-c',{child!r}],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)\n"
            "assert p.stdout.readline().strip()==b'ready'\nprint(p.pid,flush=True)\nthreading.Event().wait()\n")
    rows=[]
    observations=(E/'threaded-v3-observations.jsonl').open('x',encoding='utf8')
    for attempt in range(attempts):
        with tempfile.TemporaryDirectory(prefix='late-',dir=E) as directory:
            root=Path(directory).resolve();env=CommandPolicy(root).subprocess_environment()
            p=subprocess.Popen([sys._base_executable,'-u','-c',script],cwd=root,env=env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=0x4|subprocess.CREATE_NEW_PROCESS_GROUP)
            lines=queue.Queue();handles=[];opened=[];waited=[];triggered=[]
            def drain():
                for line in iter(p.stdout.readline,b''):lines.put(line.strip())
            reader=threading.Thread(target=drain,daemon=True)
            class Proxy:
                def __getattr__(self,name):return getattr(k,name)
            proxy=Proxy()
            # Configure original functions before installing the boundary wrappers.
            k.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
            k.QueryInformationJobObject.restype=wintypes.BOOL
            def spawn_at_boundary(job_handle,stage):
                if not triggered:
                    triggered.append(stage)
                    p.stdin.write(b'spawn\n');p.stdin.flush()
                    pid=int(lines.get(timeout=5))
                    h=k.OpenProcess(0x00101001,False,pid)
                    if not h:raise ctypes.WinError(ctypes.get_last_error())
                    handles.append((pid,h));member=wintypes.BOOL()
                    assert k.IsProcessInJob(h,job_handle,ctypes.byref(member)) and member.value
            def query(*args):
                result=k.QueryInformationJobObject(*args)
                if result and args[1]==3 and boundary=='query':spawn_at_boundary(args[0],'query')
                return result
            k.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT];k.TerminateJobObject.restype=wintypes.BOOL
            def terminate(*args):
                if boundary=='terminate':spawn_at_boundary(args[0],'terminate')
                return k.TerminateJobObject(*args)
            proxy.TerminateJobObject=Fn(terminate)
            def opened_process(*args):
                h=k.OpenProcess(*args);opened.append((args[2],h));return h
            def waited_process(*args):waited.append(args[0]);return k.WaitForSingleObject(*args)
            proxy.QueryInformationJobObject=Fn(query);proxy.OpenProcess=Fn(opened_process)
            proxy.WaitForSingleObject=Fn(waited_process)
            resource=None
            try:
                with patch.object(ctypes,'WinDLL',return_value=proxy):job=control._create_windows_job(p)
                assert job is not None
                resource=control._ProcessResources(p,env,job)
                control._resume_windows_process(p,resource)
                reader.start();resource.readers=(reader,)
                assert lines.get(timeout=5)==b'ready'
                confirmed=resource.finish(None)
                pid,h=handles[0]
                wait_now=k.WaitForSingleObject(h,0)
                removed_before_test_cleanup=False
                if confirmed and wait_now==0:
                    assert root.parent==E.resolve()
                    shutil.rmtree(root)  # Strict immediate removal, before test-owned finally.
                    removed_before_test_cleanup=not root.exists()
                rows.append({'attempt':attempt,'cleanup_confirmed':confirmed,
                             'trigger_stage':triggered[0], 'directory_removed_before_test_cleanup':removed_before_test_cleanup, 'late_child_in_exact_job':True,'late_child_opened_by_implementation':pid in [x[0] for x in opened],
                             'late_child_wait_at_return':wait_now,'implementation_process_handles_waited':len(waited)})
                observations.write(json.dumps(rows[-1])+'\n');observations.flush()
            finally:
                if resource is not None:
                    assert resource.cleanup(time.monotonic()+5)
                elif p.poll() is None:p.kill();p.wait(timeout=5)
                for pid,h in handles:
                    if k.WaitForSingleObject(h,5000)!=0:assert k.TerminateProcess(h,1)
                    assert k.WaitForSingleObject(h,5000)==0
                    assert k.CloseHandle(h)
                if reader.is_alive():reader.join(timeout=5)
                assert not reader.is_alive()
            # TemporaryDirectory performs immediate deletion; errors remain errors.
    with (E/'real-late-threaded-member-v3.json').open('x',encoding='utf8') as f:json.dump(rows,f,indent=2)
    print(json.dumps({'attempts':len(rows),'confirmed_while_child_not_signaled':sum(x['cleanup_confirmed'] and x['late_child_wait_at_return']!=0 for x in rows),
                      'late_children_not_captured':sum(not x['late_child_opened_by_implementation'] for x in rows)}))
    assert all(x['trigger_stage']==boundary for x in rows), 'wrong controlled interleaving'
    assert all(x['cleanup_confirmed'] and x['late_child_wait_at_return']==0 and x['directory_removed_before_test_cleanup'] for x in rows), 'all controlled late children must be captured and exited before success'
    return rows

class Fn:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)

def check(value,message='expected true'):
    assert value,message

def reject(value,message='expected false'):
    assert not value,message

from contextlib import contextmanager
from types import SimpleNamespace
@contextmanager
def raises_cancel():
    caught=SimpleNamespace(exception=None)
    try:yield caught
    except CancellationError as error:caught.exception=error
    else:raise AssertionError('expected CancellationError')


def cancellation(E,point="snapshot",parent_cancel=False):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateEventW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.BOOL,wintypes.LPCWSTR];k.CreateEventW.restype=wintypes.HANDLE
    k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
    k.CloseHandle.argtypes=[wintypes.HANDLE];k.CloseHandle.restype=wintypes.BOOL
    k.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    k.ResumeThread.argtypes=[wintypes.HANDLE];k.ResumeThread.restype=wintypes.DWORD
    name='Local\\TriCoderReview-'+uuid.uuid4().hex
    event=k.CreateEventW(None,True,False,name);check(event)
    parent=CancellationToken();token=parent.create_child();target=parent if parent_cancel else token;facts=[];processes=[]
    class Proxy:
        def __getattr__(self,name):return getattr(k,name)
    proxy=Proxy()
    def snapshot(*args):
        handle=k.CreateToolhelp32Snapshot(*args)

        if point=='snapshot':target.cancel()
        facts.append({'stage':'snapshot','cancelled':token.is_cancelled})
        return handle
    def resume(handle):
        cancelled=token.is_cancelled
        previous=k.ResumeThread(handle)
        # The OS event is a causal handshake, not a sleep or cleanup retry.
        state=k.WaitForSingleObject(event,5000) if previous==1 else None
        facts.append({'stage':'resume','cancelled_before_resume':cancelled,'previous_count':previous,'user_script_event':state})
        return previous
    def open_thread(*args):
        handle=k.OpenThread(*args)
        if point=='open':target.cancel()
        facts.append({'stage':'open','cancelled':token.is_cancelled})
        return handle
    k.OpenThread.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenThread.restype=wintypes.HANDLE
    proxy.CreateToolhelp32Snapshot=Fn(snapshot);proxy.OpenThread=Fn(open_thread);proxy.ResumeThread=Fn(resume)
    real_popen=control.subprocess.Popen
    def popen(*args,**kwargs):
        p=real_popen(*args,**kwargs);processes.append(p);return p
    script=("import ctypes,threading\nfrom ctypes import wintypes\n"
            "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
            "k.OpenEventW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR];k.OpenEventW.restype=wintypes.HANDLE\n"
            "k.SetEvent.argtypes=[wintypes.HANDLE];k.CloseHandle.argtypes=[wintypes.HANDLE]\n"
            f"h=k.OpenEventW(2,False,{name!r})\nassert h\nassert k.SetEvent(h)\nk.CloseHandle(h)\nthreading.Event().wait()\n")
    try:
        with tempfile.TemporaryDirectory(prefix='cancel-',dir=E) as directory:
            root=Path(directory).resolve();(root/'check.py').write_text(script,encoding='utf8')
            policy=CommandPolicy(root)
            with patch.object(ctypes,'WinDLL',return_value=proxy),patch.object(control.subprocess,'Popen',side_effect=popen):
                with raises_cancel() as caught:
                    control.run_bounded_process(policy.validate('python check.py'),cwd=root,
                        env=policy.subprocess_environment(),timeout=5,max_output_bytes=4096,cancellation=token)
            reject(caught.exception.cleanup_failed)
            check(all(p.poll() is not None for p in processes))
            facts.append({'stage':'after_cleanup','all_parents_exited':True,'cleanup_failed':caught.exception.cleanup_failed})
            with (E/'real-cancel-during-snapshot.json').open('x',encoding='utf8') as f:json.dump(facts,f,indent=2)
            reject(any(x.get('stage')=='resume' for x in facts),'ResumeThread must not be called after observed cancellation')
            reject(k.WaitForSingleObject(event,0)==0,'cancelled script must not signal')
    finally:
        check(k.CloseHandle(event))
    return facts

if __name__=='__main__':
    root=Path(sys.argv[2]).resolve();root.mkdir(parents=True,exist_ok=False)
    if sys.argv[1]=='late':late_members(root,boundary=sys.argv[3] if len(sys.argv)>3 else 'query')
    elif sys.argv[1]=='cancel':cancellation(root,sys.argv[3],sys.argv[4]=='parent')
