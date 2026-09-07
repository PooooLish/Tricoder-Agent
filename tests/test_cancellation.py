import sys
import threading
import unittest
from pathlib import Path


# 让 ``python -m unittest`` 在未安装包的源码工作树中也能直接发现 ``src``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.core.cancellation import CancellationError, CancellationToken


class CancellationTokenTests(unittest.TestCase):
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
