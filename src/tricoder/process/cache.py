"""受控 compileall 的独占缓存目录及可重试清理所有权。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import shutil
import stat
import tempfile
import threading
import time

from tricoder.task_cleanup import current_cleanup


class CacheCreationUncertain(OSError):
    """已创建目录但无法绑定其身份；不能当作完全未分配资源。"""


class CompileCache:
    def __init__(self, workspace: Path):
        base = Path(tempfile.gettempdir()).resolve()
        if base.is_relative_to(workspace.resolve()):
            raise OSError("宿主缓存位置不能位于工作区")
        self.path = Path(tempfile.mkdtemp(prefix="tricoder-compile-", dir=base))
        self._identity = None
        self._worker = None
        self._lock = threading.Lock()
        self._removed = False
        self.dependency = None
        scope = current_cleanup()
        if scope is not None:
            scope.retain(self)
        try:
            self._identity = self.path.lstat()
        except OSError as error:
            if scope is not None:
                scope.mark_failed()
            raise CacheCreationUncertain("缓存目录身份无法确认，清理归属已保留") from error

    def _delete(self):
        try:
            metadata = self.path.lstat()
            if (not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400
                or (metadata.st_dev, metadata.st_ino) != (self._identity.st_dev, self._identity.st_ino)
                or self.path.resolve() != self.path):
                return
            # 只递归清理本次创建且身份仍一致的精确目录，不接受模型路径。
            shutil.rmtree(self.path)
            self._removed = True
        except FileNotFoundError:
            # 已删除无需再次删除；父路径变化仍不会据此删其他目录。
            self._removed = not self.path.exists()
        except OSError:
            pass

    def cleanup(self, deadline: float) -> bool:
        if self._identity is None:
            # 身份缺失不能靠再次读取路径猜测所有权；交由宿主人工诊断。
            return False
        if self.dependency is not None:
            if not self.dependency.cleanup(deadline):
                return False
            self.dependency = None
        with self._lock:
            if self._removed:
                return True
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._delete, daemon=True)
                self._worker.start()
            worker = self._worker
        worker.join(max(0, deadline-time.monotonic()))
        return not worker.is_alive() and self._removed

    def finish(self, *, terminal: bool) -> bool:
        scope = current_cleanup()
        deadline = (scope.begin_termination() if scope is not None and (
            terminal or scope.started_deadline is not None) else time.monotonic()+5)
        okay = self.cleanup(deadline)
        if scope is not None:
            if okay:
                scope.discard(self)
            else:
                scope.begin_termination(deadline=deadline)
                scope.mark_failed()
                scope.retain(self)
        return okay


def run_with_compile_cache(runner, args, *, workspace: Path, env, **kwargs):
    """保持原命令/审批，仅覆盖这个子进程环境；清理后才能发布通过证据。"""
    cache = CompileCache(workspace)
    child_env = dict(env)
    child_env["PYTHONPYCACHEPREFIX"] = str(cache.path)
    try:
        result = runner(args, env=child_env, **kwargs)
    except BaseException as primary:
        cache.dependency = getattr(primary, "cleanup_resource", None)
        try:
            cache.finish(terminal=True)
        except BaseException:
            scope = current_cleanup()
            if scope is not None:
                scope.mark_failed()
                scope.retain(cache)
        raise
    cache.dependency = result.cleanup_resource
    if not cache.finish(terminal=result.timed_out or result.cleanup_failed or result.output_exceeded):
        return replace(result, cleanup_failed=True, cleanup_resource=cache)
    return result
