"""受控 compileall 缓存必须外置，不能放宽评分工作区。"""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tricoder.tools import ToolContext, ToolRegistry
from tricoder.policy import WorkspacePolicy, CommandPolicy
from tricoder.process.control import run_bounded_process
from tricoder.process.cache import CompileCache
from tricoder.task_cleanup import TaskCleanup, cleanup_scope
from tricoder.core.cancellation import CancellationError, CancellationToken


class CommandCacheTests(unittest.TestCase):
    def test_creation_identity_failure_retains_unknown_owner(self):
        scope = TaskCleanup()
        created = []
        real_create, real_lstat = tempfile.mkdtemp, Path.lstat
        def create(*args, **kwargs):
            path = real_create(*args, **kwargs)
            created.append(Path(path))
            return path
        def metadata(path, *args, **kwargs):
            if path in created:
                raise PermissionError("synthetic")
            return real_lstat(path, *args, **kwargs)
        try:
            with cleanup_scope(scope), patch("tricoder.process.cache.tempfile.mkdtemp", side_effect=create), patch.object(Path, "lstat", metadata):
                result = self.tools.execute("run_command", {"command": "python -m compileall -q app"})
            self.assertFalse(result.ok)
            self.assertTrue(scope.has_pending)
            self.assertTrue(scope.failed)
            self.assertFalse(scope.retry(time.monotonic()+.1))
        finally:
            # 身份未知的 owner 不能擅自清理；测试宿主只删除其记录的精确空目录。
            for path in created:
                path.rmdir()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "app").mkdir()
        (self.root / "app/main.py").write_text("value=1\n", encoding="utf8")
        self.tools = ToolRegistry(ToolContext(WorkspacePolicy(self.root), CommandPolicy(),
            approver=lambda *_: True, timeout=5))

    def test_real_compileall_file_and_directory_override_parent_environment(self):
        for target in ("app/main.py", "app"):
            with self.subTest(target=target):
                cached = []
                def execute(*args, **kwargs):
                    cache = Path(kwargs["env"]["PYTHONPYCACHEPREFIX"])
                    self.assertFalse(cache.is_relative_to(self.root))
                    self.assertTrue(cache.is_dir())
                    cached.append(cache)
                    result = run_bounded_process(*args, **kwargs)
                    self.assertTrue(list(cache.rglob("*.pyc")))
                    return result
                with patch.dict(os.environ, {"PYTHONPYCACHEPREFIX": str(self.root / "injected")}), patch(
                    "tricoder.tools.command.run_bounded_process", side_effect=execute
                ):
                    result = self.tools.execute("run_command", {"command": f"python -m compileall -q {target}"})
                    self.assertTrue(result.ok, result)
                    self.assertEqual(str(self.root / "injected"), os.environ["PYTHONPYCACHEPREFIX"])
                self.assertEqual(1, len(cached))
                self.assertFalse(cached[0].exists())
                self.assertFalse(list(self.root.rglob("*.pyc")))

    def test_creation_failure_does_not_start_process(self):
        with patch("tricoder.process.cache.tempfile.mkdtemp", side_effect=OSError), patch("tricoder.tools.command.run_bounded_process") as start:
            result = self.tools.execute("run_command", {"command": "python -m compileall -q app"})
        self.assertFalse(result.ok)
        start.assert_not_called()

    def test_failed_compile_still_cleans_cache(self):
        (self.root / "app/main.py").write_text("def invalid(:", encoding="utf8")
        cached = []
        def execute(*args, **kwargs):
            cached.append(Path(kwargs["env"]["PYTHONPYCACHEPREFIX"]))
            return run_bounded_process(*args, **kwargs)
        with patch("tricoder.tools.command.run_bounded_process", side_effect=execute):
            result = self.tools.execute("run_command", {"command": "python -m compileall -q app"})
        self.assertFalse(result.ok)
        self.assertEqual(1, result.command_check.returncode)
        self.assertFalse(cached[0].exists())

    def test_delete_failure_retains_owner_and_blocks_reuse_until_retry(self):
        scope = TaskCleanup()
        paths = []
        def denied(path):
            paths.append(Path(path))
            raise PermissionError("synthetic")
        with cleanup_scope(scope), patch("tricoder.process.cache.shutil.rmtree", side_effect=denied):
            result = self.tools.execute("run_command", {"command": "python -m compileall -q app"})
        self.assertFalse(result.ok)
        self.assertEqual("cleanup_failed", result.error.code.value)
        self.assertTrue(scope.failed)
        self.assertTrue(scope.has_pending)
        self.assertIsNone(result.verification_evidence)
        # 显式外层 scope 的生命周期归宿主负责，模拟宿主交接而非新建另一个 scope。
        self.tools.retain_cleanup(scope)
        self.assertEqual("cleanup_failed", self.tools.execute("list_files", {}).error.code.value)
        self.assertTrue(scope.retry(time.monotonic()+2))
        self.assertFalse(paths[0].exists())

    def test_owned_directory_replacement_is_not_recursively_deleted(self):
        cache = CompileCache(self.root)
        backup = cache.path.with_name(cache.path.name + "-moved")
        cache.path.rename(backup)
        cache.path.mkdir()
        marker = cache.path / "do-not-delete"
        marker.write_text("external", encoding="utf8")
        try:
            self.assertFalse(cache.cleanup(time.monotonic()+1))
            self.assertTrue(marker.exists())
        finally:
            # 两个精确测试目录均为空后逐项恢复，以便 owner 正常清理。
            marker.unlink()
            cache.path.rmdir()
            backup.rename(cache.path)
            self.assertTrue(cache.cleanup(time.monotonic()+1))

    def test_blocked_cleanup_retains_exact_worker(self):
        cache = CompileCache(self.root)
        entered, release = threading.Event(), threading.Event()
        import shutil
        remove = shutil.rmtree
        def delayed(path):
            entered.set()
            release.wait(3)
            remove(path)
        with patch("tricoder.process.cache.shutil.rmtree", side_effect=delayed):
            self.assertFalse(cache.cleanup(time.monotonic()+.03))
            self.assertTrue(entered.is_set())
            worker = cache._worker
            self.assertFalse(cache.cleanup(time.monotonic()+.01))
            self.assertIs(worker, cache._worker)
            release.set()
            self.assertTrue(cache.cleanup(time.monotonic()+2))

    def test_timeout_and_token_cancel_clean_real_process_and_cache(self):
        from tricoder.process.cache import run_with_compile_cache
        import sys
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                cached = []
                scope = TaskCleanup()
                token = CancellationToken()
                def execute(*args, **kwargs):
                    cached.append(Path(kwargs["env"]["PYTHONPYCACHEPREFIX"]))
                    return run_bounded_process(*args, **kwargs)
                timer = threading.Timer(.1, token.cancel)
                if cancel:
                    timer.start()
                try:
                    with cleanup_scope(scope):
                        if cancel:
                            with self.assertRaises(CancellationError):
                                run_with_compile_cache(execute, [sys.executable, "-c", "import time;time.sleep(3)"],
                                    workspace=self.root, env={}, cwd=self.root, timeout=2, max_output_bytes=100, cancellation=token)
                        else:
                            result = run_with_compile_cache(execute, [sys.executable, "-c", "import time;time.sleep(3)"],
                                workspace=self.root, env={}, cwd=self.root, timeout=.1, max_output_bytes=100)
                            self.assertTrue(result.timed_out)
                finally:
                    if cancel:
                        timer.join()
                self.assertFalse(scope.has_pending)
                self.assertFalse(cached[0].exists())

    def test_parent_temp_directory_inside_workspace_is_refused(self):
        with patch("tricoder.process.cache.tempfile.gettempdir", return_value=str(self.root)), patch("tricoder.process.cache.tempfile.mkdtemp") as create:
            with self.assertRaises(OSError):
                CompileCache(self.root)
        create.assert_not_called()
