import sys
import threading
import tempfile
import asyncio
from unittest import mock
import unittest
from pathlib import Path


# 让 ``python -m unittest`` 在未安装包的源码工作树中也能直接发现 ``src``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.core.cancellation import CancellationError, CancellationToken


class CancellationTokenTests(unittest.TestCase):
    def test_registry_without_session_runtime_retains_failed_resource_and_blocks_reuse(self):
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.tools import ToolContext, ToolRegistry
        from tricoder.execution_state import ErrorCode
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "task.py").write_text("pass\n", encoding="utf-8")
            registry = ToolRegistry(ToolContext(WorkspacePolicy(root), CommandPolicy(workspace=root), lambda *_: True))
            with mock.patch("tricoder.subprocess_control._terminate_process_tree", return_value=False):
                failed = registry.execute("run_command", {"command": "python task.py"})
            self.assertEqual(ErrorCode.CLEANUP_FAILED, failed.error.code)
            blocked = registry.execute("finish", {"summary": "must not erase cleanup failure"})
            self.assertFalse(blocked.ok)
            self.assertEqual(ErrorCode.CLEANUP_FAILED, blocked.error.code)

    def test_provider_blocking_read_is_cooperative_not_thread_termination(self):
        from tricoder.providers import UrllibTransport
        entered = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        closed = threading.Event()
        token = CancellationToken()

        class Response:
            def read1(self, _amount):
                entered.set()
                try:
                    if not release.wait(2):
                        raise TimeoutError("test read release missing")
                    return b"synthetic"
                finally:
                    exited.set()
            def close(self):
                closed.set()

        async def investigate():
            stream = UrllibTransport().post_stream("https://example.test", {}, {}, 1, token)
            task = asyncio.create_task(anext(stream))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            token.cancel()
            # 令牌取消不会强杀阻塞 Python 线程；不能把这一阶段声称为回收完成。
            self.assertFalse(exited.is_set())
            self.assertFalse(task.done())
            release.set()
            await asyncio.wait_for(task, 1)
            with self.assertRaises(CancellationError):
                await anext(stream)
            self.assertTrue(exited.is_set())
            self.assertTrue(closed.is_set())

        with mock.patch("tricoder.providers.urllib.request.urlopen", return_value=Response()):
            try:
                asyncio.run(investigate())
            finally:
                release.set()

    def test_approval_then_cancellation_never_commits_file(self):
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from tricoder.tools import ToolContext, ToolRegistry

        for asynchronous in (False, True):
            for name, arguments in (
                ("create_file", {"path": "new.txt", "content": "new"}),
                ("edit_file", {"path": "old.txt", "old_text": "old", "new_text": "new"}),
                ("apply_patch", {"patch": "--- a/old.txt\n+++ b/old.txt\n@@ -1 +1 @@\n-old\n+new\n"}),
            ):
                with self.subTest(tool=name, asynchronous=asynchronous), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    (root / "old.txt").write_text("old\n", encoding="utf-8")
                    token = CancellationToken()

                    def approve(*_args):
                        token.cancel()
                        return True

                    registry = ToolRegistry(ToolContext(WorkspacePolicy(root), CommandPolicy(), approve))
                    with self.assertRaises(CancellationError):
                        if asynchronous:
                            asyncio.run(registry.execute_async(name, arguments, cancellation=token))
                        else:
                            registry.execute(name, arguments, cancellation=token)
                    self.assertEqual("old\n", (root / "old.txt").read_text(encoding="utf-8"))
                    self.assertFalse((root / "new.txt").exists())

    def test_new_token_is_active_and_cancel_is_idempotent(self) -> None:
        """防止重复取消改变状态或产生第二次状态迁移。"""
        token = CancellationToken()

        self.assertFalse(token.is_cancelled)
        self.assertTrue(token.cancel())
        self.assertFalse(token.cancel())
        self.assertTrue(token.is_cancelled)

    def test_parent_cancellation_propagates_to_child_only(self) -> None:
        """防止父任务取消丢失，或子任务反向取消父任务。"""
        parent = CancellationToken()
        child = parent.create_child()

        self.assertTrue(child.cancel())
        self.assertTrue(child.is_cancelled)
        self.assertFalse(parent.is_cancelled)

        second_child = parent.create_child()
        parent.cancel()

        self.assertTrue(second_child.is_cancelled)

    def test_raise_if_cancelled_uses_specific_exception(self) -> None:
        """防止调用方无法把主动取消和普通运行失败区分开。"""
        token = CancellationToken()
        token.raise_if_cancelled()

        token.cancel()

        with self.assertRaises(CancellationError):
            token.raise_if_cancelled()

    def test_cancel_can_be_triggered_from_another_thread(self) -> None:
        """防止 TUI 线程触发取消时出现不可见或竞态状态。"""
        token = CancellationToken()
        worker = threading.Thread(target=token.cancel)

        worker.start()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(token.is_cancelled)


if __name__ == "__main__":
    unittest.main()
