"""Bounded subprocess capture with process-tree cleanup."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time
from typing import Any

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.task_cleanup import current_cleanup


_READ_CHUNK_BYTES = 8192
_WAIT_SLICE_SECONDS = 0.02
_CLEANUP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_exceeded: bool = False
    cleanup_failed: bool = False
    # 仅失败时保留 exact 资源；非 Runtime 调用方也不会失去后续清理归属。
    cleanup_resource: object | None = field(default=None, repr=False, compare=False)


class ProcessExecutionUncertain(OSError):
    """Popen 已返回进程，随后异常不能再当作确定未启动。"""

    def __init__(self, *, cleanup_failed: bool, cleanup_resource: object | None = None) -> None:
        super().__init__("进程执行结果无法确认")
        self.cleanup_failed = cleanup_failed
        self.cleanup_resource = cleanup_resource


class _ProcessResources:
    """持有进程、Job 和读线程，清理失败后由 Runtime 或结果继续持有。"""

    def __init__(self, process, env, windows_job=None):
        self.process = process
        self.env = env
        self.windows_job = windows_job
        self.readers: tuple[threading.Thread, ...] = ()
        self.deadline: float | None = None

    def cleanup(self, deadline: float) -> bool:
        try:
            okay = _terminate_process_tree(self.process, self.env, self.windows_job, deadline=deadline)
        except Exception:
            okay = False
        for reader in self.readers:
            if reader.is_alive():
                reader.join(timeout=max(0.0, deadline - time.monotonic()))
            if reader.is_alive():
                okay = False
        # 活读线程可能持有 BufferedReader 锁，不能在调用线程强行 close 而无界等待。
        if not any(reader.is_alive() for reader in self.readers):
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        okay = False
        return okay

    def finish(self, cancellation: CancellationToken | None, *, terminal: bool = False) -> bool:
        scope = current_cleanup()
        terminal = terminal or (cancellation is not None and cancellation.is_cancelled)
        if scope is not None and (terminal or scope.started_deadline is not None):
            self.deadline = scope.begin_termination(deadline=self.deadline)
        if self.deadline is None:
            # 成功命令的例行收尾是局部资源预算，不限制仍正常运行的长任务。
            self.deadline = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
        okay = self.cleanup(self.deadline)
        if not okay and scope is not None:
            # 局部收尾失败后任务必须终止；沿用已消费的期限，不能再给 MCP 5 秒。
            scope.begin_termination(deadline=self.deadline)
            scope.mark_failed()
            scope.retain(self)
        return okay


def run_bounded_process(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
    cancellation: CancellationToken | None = None,
) -> BoundedProcessResult:
    """Run without a shell while retaining at most ``max_output_bytes`` total."""

    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        **popen_options,
    )
    windows_job: _WindowsJob | None = None
    resources = _ProcessResources(process, env)
    try:
        windows_job = _create_windows_job(process) if os.name == "nt" else None
        resources.windows_job = windows_job
        if os.name == "nt" and windows_job is None:
            raise OSError("无法建立 Windows 进程树约束")
        return _collect_bounded_process(
            process,
            env=env,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            windows_job=windows_job,
            cancellation=cancellation,
            resources=resources,
        )
    except BaseException as primary:
        if getattr(primary, "cleanup_resource", None) is resources:
            raise
        try:
            cleanup_ok = resources.finish(cancellation, terminal=True)
        except BaseException:
            # 清理中的第二个中断不能覆盖此前业务异常；资源仍交回任务所有者。
            cleanup_ok = False
            scope = current_cleanup()
            if scope is not None:
                scope.mark_failed()
                scope.retain(resources)
        if isinstance(primary, OSError):
            raise ProcessExecutionUncertain(cleanup_failed=not cleanup_ok,
                                            cleanup_resource=resources if not cleanup_ok else None) from primary
        if not cleanup_ok:
            primary.cleanup_failed = True
            primary.cleanup_resource = resources
        # 取消与未知编程异常仍由既有上层边界处理，清理不取代根因。
        raise


def _collect_bounded_process(
    process: subprocess.Popen[bytes],
    *,
    env: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
    windows_job: "_WindowsJob | None",
    cancellation: CancellationToken | None = None,
    resources: _ProcessResources | None = None,
) -> BoundedProcessResult:
    assert process.stdout is not None
    assert process.stderr is not None
    resources = resources or _ProcessResources(process, env, windows_job)

    lock = threading.Lock()
    exceeded = threading.Event()
    captured_bytes = [0]
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream, target: bytearray) -> None:  # type: ignore[no-untyped-def]
        try:
            while not exceeded.is_set():
                # ``BufferedReader.read(size)`` 可能等待凑满 size 或等到 EOF，
                # 导致“仅超限 1 字节后挂起”的进程直到 timeout 才被发现。
                # ``read1`` 每次只进行一次底层读取，能立即处理管道中现有数据。
                chunk = stream.read1(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                with lock:
                    remaining = max_output_bytes - captured_bytes[0]
                    if remaining > 0:
                        kept = chunk[:remaining]
                        target.extend(kept)
                        captured_bytes[0] += len(kept)
                    if len(chunk) > remaining:
                        exceeded.set()
                        return
        finally:
            stream.close()

    readers = (
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    )
    resources.readers = readers
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    cancelled = False
    while process.poll() is None:
        if cancellation is not None and cancellation.is_cancelled:
            cancelled = True
            break
        if exceeded.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            process.wait(timeout=min(_WAIT_SLICE_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            continue

    if not exceeded.is_set() and not timed_out and not cancelled:
        process.wait()
    cleanup_failed = not resources.finish(
        cancellation, terminal=cancelled or timed_out or exceeded.is_set(),
    )

    if cancelled:
        error = CancellationError("操作已取消", cleanup_failed=cleanup_failed)
        error.cleanup_resource = resources
        raise error

    return BoundedProcessResult(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_exceeded=exceeded.is_set(),
        cleanup_failed=cleanup_failed,
        cleanup_resource=resources if cleanup_failed else None,
    )


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    env: Mapping[str, str],
    windows_job: "_WindowsJob | None",
    *,
    deadline: float | None = None,
) -> bool:
    deadline = deadline if deadline is not None else time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
    cleanup_ok = True
    if os.name == "nt":
        if windows_job is not None:
            cleanup_ok = windows_job.close()
        else:
            cleanup_ok = _terminate_windows_tree(process, env, deadline=deadline)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_ok = False
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            cleanup_ok = False
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        cleanup_ok = False
    return cleanup_ok and process.poll() is not None


class _WindowsJob:
    def __init__(self, handle: int, close_handle: Any) -> None:
        self._handle = handle
        self._close_handle = close_handle

    def close(self) -> bool:
        if not self._handle:
            return True
        handle = self._handle
        if not self._close_handle(handle):
            return False
        # 只有确认 CloseHandle 成功才放弃句柄所有权；失败可由原资源持有者重试。
        self._handle = 0
        return True


def _create_windows_job(
    process: subprocess.Popen[bytes],
) -> _WindowsJob | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        set_information.restype = wintypes.BOOL
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign_process.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_job(None, None)
        if not handle:
            return None
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not set_information(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            close_handle(handle)
            return None
        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not assign_process(handle, process_handle):
            close_handle(handle)
            return None
        return _WindowsJob(int(handle), close_handle)
    except (AttributeError, OSError, ValueError):
        return None


def _terminate_windows_tree(
    process: subprocess.Popen[bytes],
    env: Mapping[str, str],
    *,
    deadline: float | None = None,
) -> bool:
    deadline = deadline if deadline is not None else time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
    system_root = env.get("SystemRoot") or env.get("SYSTEMROOT")
    if not system_root:
        return False
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    try:
        metadata = taskkill.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or taskkill.is_symlink()
        or (reparse_flag and attributes & reparse_flag)
    ):
        return False
    try:
        completed = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(0.0, deadline - time.monotonic()),
            shell=False,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
