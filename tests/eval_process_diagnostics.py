"""Safe test-only receipts for verifier failures; no argv, env or output."""
import json
import sys
import time
from unittest.mock import patch

from tricoder.process import control


def exception_fields(error):
    kinds = (PermissionError, FileNotFoundError, OSError, RuntimeError, ValueError)
    cause = error.__cause__
    cause = cause if isinstance(cause, OSError) else error
    return {
        'exception_type': next((k.__name__ for k in kinds if isinstance(error, k)), 'other'),
        'errno': getattr(cause, 'errno', None) if type(getattr(cause, 'errno', None)) is int else None,
        'winerror': getattr(cause, 'winerror', None) if type(getattr(cause, 'winerror', None)) is int else None,
    }


class ProcessDiagnostics:
    def __init__(self):
        self.events = []

    def __enter__(self):
        finish = control._ProcessResources.finish
        def observed(resource, *args, **kwargs):
            event = {'stage': 'process_finish', 'pid': resource.process.pid}
            try:
                result = finish(resource, *args, **kwargs)
                event['cleanup_confirmed'] = result
                return result
            except BaseException as error:
                event.update(exception_fields(error))
                raise
            finally:
                event.update(
                    returncode=resource.process.poll(),
                    readers_alive=sum(t.is_alive() for t in resource.readers),
                    streams_closed=[s.closed for s in (resource.process.stdout, resource.process.stderr, resource.process.stdin) if s is not None],
                    job_present=resource.windows_job is not None,
                    job_handle_open=bool(getattr(resource.windows_job, '_handle', 0)),
                    deadline_expired=resource.deadline is not None and resource.deadline <= time.monotonic(),
                )
                self.events.append(event)
        self.patch = patch.object(control._ProcessResources, 'finish', observed)
        self.patch.start()
        return self

    def emit(self, stage, error=None):
        value = {'stage': stage, 'processes': self.events}
        if error is not None:
            value.update(exception_fields(error))
        print('eval_process_diagnostic=' + json.dumps(value, sort_keys=True), file=sys.stderr)

    def __exit__(self, kind, error, traceback):
        self.patch.stop()
        if error is not None:
            self.emit('test_exit', error)
