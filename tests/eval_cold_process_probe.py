"""Standalone cold-start probe. Only emits safe process facts; owns its children."""
import sys,json,os,time,tempfile,shutil
from pathlib import Path
from tricoder.policy import CommandPolicy
from tricoder.process.control import run_bounded_process
root=Path(sys.argv[1]).resolve();root.mkdir()
child="import os,threading;from pathlib import Path;Path('actual.pid').write_text(str(os.getpid()));print('ready',flush=True);threading.Event().wait()"
(root/'check.py').write_text('import subprocess,sys\nfrom pathlib import Path\n'+f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)\n"+"Path('launcher.pid').write_text(str(p.pid))\np.stdout.readline()\nraise SystemExit(1)\n",encoding='utf8')
policy=CommandPolicy(root);args=policy.validate('python check.py')
receipt={'ctypes_loaded_before_process':'ctypes' in sys.modules}
from tricoder.process import control
original_finish=control._ProcessResources.finish
def observed_finish(resource,*args,**kwargs):
 import ctypes
 from ctypes import wintypes
 k=ctypes.WinDLL('kernel32',use_last_error=True)
 k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
 k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)];k.IsProcessInJob.restype=wintypes.BOOL
 k.CloseHandle.argtypes=[wintypes.HANDLE]
 receipt['membership']=[]
 for name in ('launcher.pid','actual.pid'):
  if not (root/name).exists():continue
  pid=int((root/name).read_text());h=k.OpenProcess(0x1000,False,pid);member=wintypes.BOOL()
  okay=k.IsProcessInJob(h,resource.windows_job._handle,ctypes.byref(member)) if h and resource.windows_job else False
  receipt['membership'].append({'kind':name,'query_ok':bool(okay),'in_exact_job':bool(member.value)})
  if h:k.CloseHandle(h)
 return original_finish(resource,*args,**kwargs)
control._ProcessResources.finish=observed_finish
r=run_bounded_process(args,cwd=root,env=policy.subprocess_environment(),timeout=5,max_output_bytes=4096)
receipt.update(returncode=r.returncode,timed_out=r.timed_out,cleanup_failed=r.cleanup_failed,resource_returned=r.cleanup_resource is not None)
import ctypes
from ctypes import wintypes
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
k.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];k.TerminateProcess.restype=wintypes.BOOL
k.CloseHandle.argtypes=[wintypes.HANDLE]
receipt['children']=[]
for name in ('launcher.pid','actual.pid'):
 if not (root/name).exists():continue
 pid=int((root/name).read_text());h=k.OpenProcess(0x00100001,False,pid)
 row={'kind':name,'pid':pid,'opened':bool(h)}
 if h:
  row['wait_before_cleanup']=k.WaitForSingleObject(h,0)
  row['wait_bounded_observation']=k.WaitForSingleObject(h,1000)
  if row['wait_bounded_observation']!=0:row['test_owned_termination']=bool(k.TerminateProcess(h,1))
  row['wait_after_cleanup']=k.WaitForSingleObject(h,5000);k.CloseHandle(h)
 receipt['children'].append(row)
shutil.rmtree(root);receipt['directory_removed']=not root.exists()
print(json.dumps(receipt))
