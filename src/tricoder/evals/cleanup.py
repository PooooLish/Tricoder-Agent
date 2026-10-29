"""Verifier ownership lives outside serializable evaluation results.

Standalone callers use a process-lifetime owner. Embedders may supply their
own owner, retain it after a run, and explicitly retry pending cleanup. Neither
handoff nor retry grants a failed verifier a new termination budget.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

from tricoder.task_cleanup import TaskCleanup, cleanup_scope, current_cleanup


_process_owner = TaskCleanup()
_owner: ContextVar[TaskCleanup | None] = ContextVar('eval_verifier_owner', default=None)


def verification_cleanup_owner() -> TaskCleanup:
    return _owner.get() or _process_owner


@contextmanager
def verification_owner_scope(owner: TaskCleanup | None):
    token = _owner.set(owner or verification_cleanup_owner())
    try:
        yield
    finally:
        _owner.reset(token)


class _PendingVerification:
    def __init__(self, scope: TaskCleanup):
        self.scope = scope

    def cleanup(self, deadline: float) -> bool:
        # TaskCleanup.retry itself accepts its caller's budget. Clamp at this
        # boundary so even a later host retry cannot extend the verifier's one.
        return self.scope.retry(self.scope.begin_termination(deadline=deadline))


def retry_verification_cleanup(owner: TaskCleanup | None = None) -> bool:
    owner = owner or verification_cleanup_owner()
    deadline = owner.started_deadline
    return owner.retry(deadline if deadline is not None else time.monotonic() + 5.0)


@contextmanager
def verifier_scope():
    parent = current_cleanup()
    scope = parent or TaskCleanup()
    with cleanup_scope(scope):
        try:
            yield scope
        finally:
            if parent is None and scope.has_pending:
                owner = verification_cleanup_owner()
                owner.mark_failed()
                owner.retain(_PendingVerification(scope))


def retain_failure(scope: TaskCleanup, source: object) -> None:
    resource = getattr(source, 'cleanup_resource', None)
    if resource is None:
        return
    # A cancellation may return an already closed resource. Retaining then
    # confirming it is safe; simply discarding the exception's object is not.
    scope.retain(resource)
    scope.mark_failed()
    deadline = getattr(resource, 'deadline', None)
    scope.retry(scope.begin_termination(deadline=deadline))
