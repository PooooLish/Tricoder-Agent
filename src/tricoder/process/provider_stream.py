"""urllib 流的精确资源所有者；取消等待不等于阻塞线程已经退出。"""
from __future__ import annotations

import asyncio
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

from tricoder.task_cleanup import current_cleanup


class AsyncStreamOwner:
    """只在协程真正结束后才解除其所有权，包括自定义传输的关闭协程。"""

    def __init__(self, task):
        self.task = task

    def cleanup(self, deadline):
        if not self.task.done():
            return False
        if not self.task.cancelled():
            self.task.exception()
        return True


async def finish_async_operation(task, pending, *, cancel=False, deadline=None):
    scope = current_cleanup()
    deadline = (scope.begin_termination(deadline=deadline) if scope is not None
                else deadline if deadline is not None else time.monotonic() + 5)
    if cancel:
        task.cancel()
    owner = AsyncStreamOwner(task)
    # 先登记再等待：等待期间再次取消，也不能丢失未退出的读取/关闭协程。
    pending.append(owner)
    if scope is not None:
        scope.retain(owner)
    try:
        await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
    finally:
        cleaned = owner.cleanup(deadline)
        if cleaned:
            pending.remove(owner)
            if scope is not None:
                scope.discard(owner)
        elif scope is not None:
            scope.mark_failed()
    return cleaned


class UrllibStreamOwner:
    """每个请求一个连接/读取线程与关闭线程，故障时留给原 TaskCleanup。"""

    def __init__(self, request, timeout):
        self.request, self.timeout = request, timeout
        self.queue = queue.Queue(maxsize=2)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.response = None
        self.closer = None
        self.closed = False
        self.worker = threading.Thread(target=self._run, daemon=True)

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.queue.put(value, timeout=.02)
                return
            except queue.Full:
                continue

    def _run(self):
        try:
            try:
                response = urllib.request.urlopen(self.request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                # HTTPError 同时持有响应体，也必须交给同一关闭所有者。
                self.response = error
                raise
            self.response = response
            read = getattr(response, "read1", None) or response.read
            while not self.stop.is_set():
                chunk = read(64 * 1024)
                self._put(chunk)
                if not chunk:
                    break
        except Exception as error:
            self._put(error)
        finally:
            self._start_close()

    def _start_close(self):
        with self.lock:
            if self.response is None or self.closer is not None:
                return
            self.closer = threading.Thread(target=self._close, daemon=True)
            self.closer.start()

    def _close(self):
        try:
            # 真实 HTTPResponse 的 close 可能等待读取锁；先中断套接字读。
            fp = getattr(self.response, "fp", None)
            raw = getattr(fp, "raw", None)
            sock = getattr(raw, "_sock", None)
            if isinstance(sock, socket.socket):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.response.close()
            self.closed = True
        except Exception:
            # 不泄漏底层异常正文；closed=False 保留故障事实与所有权。
            self.closed = False

    def cleanup(self, deadline):
        self.stop.set()
        self._start_close()
        if self.worker.ident is not None:
            self.worker.join(max(0, deadline - time.monotonic()))
        closer = self.closer
        if closer is not None:
            closer.join(max(0, deadline - time.monotonic()))
        return (not self.worker.is_alive() and (closer is None or not closer.is_alive())
                and (self.response is None or self.closed))

    async def finish(self, *, terminal):
        scope = current_cleanup()
        deadline = (scope.begin_termination() if scope is not None and
                    (terminal or scope.started_deadline is not None) else time.monotonic() + 5)
        while True:
            if self.cleanup(time.monotonic()):
                if scope is not None:
                    scope.discard(self)
                return True
            if time.monotonic() >= deadline:
                if scope is not None:
                    scope.begin_termination(deadline=deadline)
                    scope.mark_failed()
                    scope.retain(self)
                return False
            await asyncio.sleep(.01)
