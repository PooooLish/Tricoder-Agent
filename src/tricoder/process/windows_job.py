"""Windows Job lifecycle ledger; notifications discover, accounting reconciles.

No notification or active-count observation is an exit certificate. Handles are
kept until all lifetime members are covered AND individually signaled. This
module is instantiated only for an owned, unnamed Job with a suspended root.
"""
import ctypes
from ctypes import wintypes as w
import threading
import time


_MAX_MEMBERS = 4096


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_longlong * 4), ("faults", w.DWORD),
                ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]


class _Ids(ctypes.Structure):
    _fields_ = [("assigned", w.DWORD), ("count", w.DWORD),
                ("pids", ctypes.c_size_t * _MAX_MEMBERS)]


class _PortInfo(ctypes.Structure):
    _fields_ = [("key", ctypes.c_void_p), ("port", w.HANDLE)]


class WindowsJobTracker:
    def __init__(self, job, process, kernel):
        # No native acquisition here: caller attaches us to Job before start().
        self.job, self.process, self.k = job, process, kernel
        self.members = {process.pid: int(process._handle)}  # borrowed from retained Popen
        self.root_pid = process.pid
        self.extras = []
        self.port = None
        self.worker = None
        self.start_attempted = False
        # DuplicateHandle writes directly into an owner-held slot. Even an
        # interruption after native acquisition cannot orphan the identity.
        self.observer_handle = w.HANDLE()
        self.observer_ready = threading.Event()
        self.observer_done = threading.Event()
        self.observer_exited = False
        self.lock = threading.Lock()
        self.capture_uncertain = False
        self.protocol_failed = False
        self.maximum_total = 1
        self.stop_sent = False
        self.complete = False

    def start(self, *, deadline=None, cancellation=None):
        deadline = time.monotonic() + 5.0 if deadline is None else deadline
        signatures = {
            "CreateIoCompletionPort": ([w.HANDLE, w.HANDLE, ctypes.c_size_t, w.DWORD], w.HANDLE),
            "GetQueuedCompletionStatus": ([w.HANDLE, ctypes.POINTER(w.DWORD),
                ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), w.DWORD], w.BOOL),
            "PostQueuedCompletionStatus": ([w.HANDLE, w.DWORD, ctypes.c_size_t, ctypes.c_void_p], w.BOOL),
            "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            "GetCurrentProcess": ([], w.HANDLE),
            "GetCurrentThread": ([], w.HANDLE),
            "DuplicateHandle": ([w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE),
                w.DWORD, w.BOOL, w.DWORD], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.k, name)
            function.argtypes, function.restype = args, result
        self.port = self.k.CreateIoCompletionPort(ctypes.c_void_p(-1), None, 0, 1)
        if not self.port:
            raise OSError("无法建立进程成员观察")
        info = _PortInfo(1, self.port)
        if not self.k.SetInformationJobObject(self.job._handle, 7, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("无法关联进程成员观察")
        self.worker = threading.Thread(target=self._observe, daemon=True)
        # Once start is attempted, neither its exception nor Python's ident /
        # is_alive state proves the native thread was never created.
        self.start_attempted = True
        self.worker.start()
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            if self.observer_ready.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("进程成员观察启动未确认")
            self.observer_ready.wait(min(0.02, remaining))
        if not self.observer_handle.value or self.observer_done.is_set():
            raise OSError("进程成员观察不可用")

    def _observe(self):
        try:
            process = self.k.GetCurrentProcess()
            # Called by the observer, never by its creator. SYNCHRONIZE only;
            # non-inheritable, no CLOSE_SOURCE and no ownership of pseudo handles.
            if not self.k.DuplicateHandle(process, self.k.GetCurrentThread(), process,
                    ctypes.byref(self.observer_handle), 0x00100000, False, 0):
                return
            self.observer_ready.set()
            self._monitor()
        except BaseException:
            self.protocol_failed = True
        finally:
            self.observer_ready.set()
            # Final tracker operation. This is quiescence, NOT native exit.
            self.observer_done.set()

    def _capture(self, pid):
        with self.lock:
            if pid in self.members:
                # Retained handles prevent the associated PID being recycled.
                return
            if len(self.members) >= _MAX_MEMBERS or len(self.extras) >= _MAX_MEMBERS:
                self.protocol_failed = True
                return
        # Never hold the ledger lock across native calls: a stalled observer must
        # not prevent the cleanup caller from returning at its original deadline.
        handle = self.k.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            self.capture_uncertain = True
            return
        with self.lock:
            self.extras.append(handle)
        member = w.BOOL()
        if not self.k.IsProcessInJob(handle, self.job._handle, ctypes.byref(member)) or not member.value:
            self.capture_uncertain = True
            return
        with self.lock:
            if pid in self.members:
                # Another producer captured the same object meanwhile. Retain
                # this duplicate in extras until the observer has stopped.
                return
            if len(self.members) >= _MAX_MEMBERS:
                self.protocol_failed = True
                return
            self.members[pid] = handle
            self.extras.remove(handle)

    def _monitor(self):
        try:
            while True:
                message, key, pid = w.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
                if not self.k.GetQueuedCompletionStatus(self.port, ctypes.byref(message),
                        ctypes.byref(key), ctypes.byref(pid), 0xffffffff):
                    self.protocol_failed = True
                    return
                if key.value == 2:
                    return
                if key.value != 1:
                    self.protocol_failed = True
                    continue
                if message.value in (6, 7, 8) and pid.value:  # NEW / EXIT / ABNORMAL_EXIT
                    self._capture(pid.value)
        except BaseException:
            # The retained Job owns this stopped observer and all acquired handles.
            self.protocol_failed = True

    def _snapshot(self, handle):
        ids = _Ids()
        if (not self.k.QueryInformationJobObject(handle, 3, ctypes.byref(ids), ctypes.sizeof(ids), None)
                or ids.assigned > _MAX_MEMBERS or ids.count > _MAX_MEMBERS
                or ids.count != ids.assigned):
            self.capture_uncertain = True
            return
        for pid in ids.pids[:ids.count]:
            self._capture(pid)

    def _stop_monitor(self, deadline):
        if not self.start_attempted:
            return True
        if self.observer_exited:
            return True
        if not self.stop_sent and not self.observer_done.is_set():
            if not self.k.PostQueuedCompletionStatus(self.port, 0, 2, None):
                return False
            self.stop_sent = True
        if not self.observer_ready.wait(max(0.0, deadline - time.monotonic())):
            return False
        handle = self.observer_handle.value
        if not handle or not self._wait(handle, deadline):
            return False
        if not self.observer_done.is_set():
            return False
        self.observer_exited = True
        return True

    def _wait(self, handle, deadline):
        return self.k.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000))) == 0

    def _close_owned(self):
        if self.start_attempted and not self.observer_exited:
            return False
        okay = True
        if self.observer_handle.value:
            if self.k.CloseHandle(self.observer_handle.value):
                self.observer_handle.value = None
            else:
                okay = False
        # Members remain in the lifetime ledger after closing. At this stage all
        # are signaled and completeness is proven, so no internal creator remains.
        for pid, handle in tuple(self.members.items()):
            if pid == self.root_pid or handle is None:
                continue
            if self.k.CloseHandle(handle):
                self.members[pid] = None
            else:
                okay = False
        for handle in tuple(self.extras):
            if self.k.CloseHandle(handle):
                self.extras.remove(handle)
            else:
                okay = False
        if self.port:
            if self.k.CloseHandle(self.port):
                self.port = None
            else:
                okay = False
        return okay

    def cleanup(self, handle, deadline):
        if self.complete:
            return self._close_owned()
        self._snapshot(handle)
        terminated = bool(self.k.TerminateJobObject(handle, 1))
        with self.lock:
            held = tuple(self.members.values())
        for process_handle in held:
            self._wait(process_handle, deadline)
        # No writer may outlive the completeness proof or resource release.
        if not self._stop_monitor(deadline):
            return False
        if not terminated or self.protocol_failed:
            return False
        if not all(self._wait(h, deadline) for h in self.members.values()):
            return False
        accounting = _Accounting()
        if not self.k.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            self.capture_uncertain = True
            return False
        if accounting.total < self.maximum_total or accounting.total > _MAX_MEMBERS:
            self.protocol_failed = True
            return False
        self.maximum_total = accounting.total
        if accounting.total != len(self.members):
            self.capture_uncertain = True
            return False
        # TotalProcesses is cumulative including exited/failed associations. It
        # detects missed messages/late births; it does NOT prove exit. Every unique
        # ledger member was independently waited above, with its original handle
        # retained since capture. No live internal process remains to create a
        # member after this read. The Job is unnamed/non-inheritable and only this
        # owner assigns the suspended root; foreign assignment is not supported.
        self.capture_uncertain = False
        self.complete = True
        return self._close_owned()
