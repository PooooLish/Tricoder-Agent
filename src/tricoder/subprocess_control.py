"""Bounded subprocess capture with process-tree cleanup."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time
from typing import Any

from tricoder.core.cancellation import CancellationError, CancellationToken


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
    try:
        windows_job = _create_windows_job(process) if os.name == "nt" else None
        if os.name == "nt" and windows_job is None:
            raise OSError("无法建立 Windows 进程树约束")
        return _collect_bounded_process(
            process,
            env=env,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            windows_job=windows_job,
            cancellation=cancellation,
        )
    except BaseException:
        _terminate_process_tree(process, env, windows_job)
        raise


def _collect_bounded_process(
    process: subprocess.Popen[bytes],
    *,
    env: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
    windows_job: "_WindowsJob | None",
    cancellation: CancellationToken | None = None,
) -> BoundedProcessResult:
    assert process.stdout is not None
    assert process.stderr is not None

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
    cleanup_failed = not _terminate_process_tree(process, env, windows_job)

    for reader in readers:
        reader.join(timeout=_CLEANUP_TIMEOUT_SECONDS)
        if reader.is_alive():
            cleanup_failed = True

    if cancelled:
        if cleanup_failed:
            raise OSError("取消后无法确认进程树已终止")
        raise CancellationError("操作已取消")

    return BoundedProcessResult(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_exceeded=exceeded.is_set(),
        cleanup_failed=cleanup_failed,
    )


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    env: Mapping[str, str],
    windows_job: "_WindowsJob | None",
) -> bool:
    cleanup_ok = True
    if os.name == "nt":
        if windows_job is not None:
            cleanup_ok = windows_job.close()
        else:
            cleanup_ok = _terminate_windows_tree(process, env)
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
        process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
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
        self._handle = 0
        return bool(self._close_handle(handle))


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
) -> bool:
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
            timeout=_CLEANUP_TIMEOUT_SECONDS,
            shell=False,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
