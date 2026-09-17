"""当前任务的清理截止时间、黏着失败事实与未确认资源所有权。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import asyncio
import threading
import time
from typing import Callable, Protocol


class CleanupResource(Protocol):
    def cleanup(self, deadline: float) -> bool: ...


class TaskCleanup:
    def __init__(self, budget: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._budget = budget
        self._deadline: float | None = None
        self._failed = False
        self._resources: list[CleanupResource] = []
        self._workers: set[_WorkerLease] = set()
        self._owners: set[TaskCleanup] = set()

    def deadline(self) -> float:
        """兼容既有清理调用；读取该接口即表示进入任务终止清理。"""
        return self.begin_termination()

    def begin_termination(self, *, deadline: float | None = None) -> float:
        """一次性进入终止清理；已消费的局部预算只能收紧，不能重新领取。"""
        with self._lock:
            candidate = time.monotonic() + self._budget if deadline is None else deadline
            if self._deadline is None:
                self._deadline = candidate
            elif deadline is not None:
                self._deadline = min(self._deadline, deadline)
            return self._deadline

    @property
    def started_deadline(self) -> float | None:
        with self._lock:
            return self._deadline

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._resources or self._workers)

    def mark_failed(self) -> None:
        with self._lock:
            self._failed = True

    def retain(self, resource: CleanupResource) -> None:
        with self._lock:
            if not any(item is resource for item in self._resources):
                self._resources.append(resource)

    def discard(self, resource: CleanupResource) -> None:
        with self._lock:
            self._resources = [item for item in self._resources if item is not resource]

    def handoff_to(self, owner: TaskCleanup) -> None:
        if owner is self:
            return
        with self._lock:
            if self._resources or self._workers:
                self._handoff_locked(owner)

    def _handoff_locked(self, owner: TaskCleanup) -> None:
        # 锁顺序固定为任务 scope → 持久 owner；owner 的 retry 不持锁调用子资源。
        self._failed = True
        self._owners.add(owner)
        owner.mark_failed()
        owner.retain(self)

    def blocks(self, scope: TaskCleanup | None) -> bool:
        with self._lock:
            resources = tuple(self._resources)
        # 同一任务允许多个并发 worker；其他任务或已登记的失败 scope 必须阻断。
        active_same_task = scope is not None and not scope.failed
        return any(not isinstance(item, _WorkerLease) or item.scope is not scope or not active_same_task
                   for item in resources)

    def start_worker(self, owner: TaskCleanup) -> _WorkerLease:
        lease = _WorkerLease(self, owner)
        with self._lock:
            self._workers.add(lease)
            # 在 executor 提交前持久 owner 已强持有唯一 lease；lease 再持有 exact scope。
            owner.retain(lease)
        return lease

    def _finish_worker(self, lease: _WorkerLease) -> None:
        with self._lock:
            if lease not in self._workers:
                return
            self._workers.remove(lease)
            if self._resources:
                self._handoff_locked(lease.owner)
            self._release_idle_owners_locked()
            lease.owner.discard(lease)

    def _release_idle_owners_locked(self) -> None:
        if not self._resources and not self._workers:
            for owner in self._owners:
                owner.discard(self)
            self._owners.clear()

    def retry(self, deadline: float) -> bool:
        with self._lock:
            if self._workers:
                # worker 尚可写入/登记资源，不能并发回收它正在使用的进程/流。
                return False
            resources = tuple(self._resources)
        for resource in resources:
            try:
                closed = resource.cleanup(deadline)
            except Exception:
                closed = False
            if closed:
                # 回调只移除 exact 旧资源；绝不更新 Session 或当前任务状态。
                with self._lock:
                    self._resources = [item for item in self._resources if item is not resource]
        with self._lock:
            self._release_idle_owners_locked()
        return not self.has_pending

    def cleanup(self, deadline: float) -> bool:
        """允许持久宿主保留整个旧任务 scope，重试时仍消费宿主的同一期限。"""
        return self.retry(deadline)


_current: ContextVar[TaskCleanup | None] = ContextVar("task_cleanup", default=None)
_worker_owner: ContextVar[TaskCleanup | None] = ContextVar("worker_cleanup_owner", default=None)


class _WorkerLease:
    def __init__(self, scope: TaskCleanup, owner: TaskCleanup) -> None:
        self.scope = scope
        self.owner = owner
        self._started = False
        self._abandoned = False

    def begin(self) -> bool:
        with self.scope._lock:
            if self._abandoned or self not in self.scope._workers:
                return False
            self._started = True
            return True

    def abandon(self) -> None:
        with self.scope._lock:
            # caller 取消与真正开始共用一把锁：排队任务不得在取消后开始产生副作用。
            self._abandoned = True
            self.scope._failed = True

    def submission_cancelled(self) -> None:
        with self.scope._lock:
            self._abandoned = True
            unstarted = not self._started
        if unstarted:
            self.finish()

    def finish(self) -> None:
        self.scope._finish_worker(self)

    def cleanup(self, deadline: float) -> bool:
        with self.scope._lock:
            return self not in self.scope._workers


async def run_in_cleanup_thread(function, /, *args, **kwargs):
    """取消 awaiter 不代表线程结束；仅真正 worker 的 finally 可以完成 lease。"""
    scope, owner = current_cleanup(), _worker_owner.get()
    if scope is None or owner is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    context = copy_context()
    lease = scope.start_worker(owner)
    def worker():
        try:
            if lease.begin():
                return context.run(function, *args, **kwargs)
        finally:
            lease.finish()
    try:
        future = asyncio.get_running_loop().run_in_executor(None, worker)
    except BaseException:
        # 提交失败时没有 worker 会执行 finally，提交者必须撤销自己的唯一 lease。
        lease.finish()
        raise
    def consume(done):
        if done.cancelled():
            # shield 阻止 caller 取消；这里只处理 executor 拒绝尚未开始的 queued work。
            lease.submission_cancelled()
        else:
            done.exception()
    future.add_done_callback(consume)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        lease.abandon()
        raise


def current_cleanup() -> TaskCleanup | None:
    return _current.get()


@contextmanager
def cleanup_scope(scope: TaskCleanup):
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


@contextmanager
def task_cleanup_scope(retain: Callable[[TaskCleanup], None], *, worker_owner: TaskCleanup | None = None):
    """复用宿主的单任务期限；独立入口每次新建，结束只交接 pending 所有权。"""
    owner_token = _worker_owner.set(worker_owner) if worker_owner is not None else None
    try:
        parent = current_cleanup()
        if parent is not None:
            yield parent
            return
        scope = TaskCleanup()
        with cleanup_scope(scope):
            try:
                yield scope
            finally:
                # 原子交接与 worker 收尾共享 scope 锁，不能迟到登记一个已 clean 的 scope。
                if scope.has_pending:
                    scope.mark_failed()
                    retain(scope)
    finally:
        if owner_token is not None:
            _worker_owner.reset(owner_token)
