"""一次性审批终态；事件和屏障控制竞争，不猜测调度窗口。"""

import importlib
import threading
import time
import unittest

from tricoder.core.cancellation import CancellationToken


class ApprovalWaitTests(unittest.TestCase):
    def make_wait(self):
        try:
            module = importlib.import_module("tricoder.approval_wait")
        except ModuleNotFoundError:
            self.fail("ApprovalWait 接口缺失")
        return module.ApprovalWait()

    def test_close_cannot_be_overridden_by_late_approval(self):
        pending = self.make_wait()
        self.assertTrue(pending.close())
        self.assertFalse(pending.resolve(True))
        self.assertFalse(pending.wait(CancellationToken(), timeout=0.01))

    def test_already_cancelled_wait_denies_and_seals(self):
        token = CancellationToken()
        token.cancel()
        pending = self.make_wait()
        self.assertFalse(pending.wait(token))
        self.assertFalse(pending.resolve(True))

    def test_timeout_denies_and_seals(self):
        pending = self.make_wait()
        started = time.monotonic()
        self.assertFalse(pending.wait(CancellationToken(), timeout=0.01))
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(pending.resolve(True))

    def test_explicit_decisions(self):
        for approved in (False, True):
            pending = self.make_wait()
            self.assertTrue(pending.resolve(approved))
            self.assertEqual(approved, pending.wait(CancellationToken()))
            self.assertFalse(pending.close())

    def test_concurrent_resolution_has_only_one_winner(self):
        pending = self.make_wait()
        barrier = threading.Barrier(9)
        results = []

        def resolve():
            barrier.wait(timeout=2)
            results.append(pending.resolve(True))

        workers = [threading.Thread(target=resolve) for _ in range(8)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(1, sum(results))
        self.assertTrue(pending.wait(CancellationToken()))

    def test_waiting_cancellation_returns_within_one_second(self):
        pending = self.make_wait()
        entered = threading.Event()
        token = CancellationToken()
        results = []

        def wait():
            entered.set()
            results.append(pending.wait(token))

        worker = threading.Thread(target=wait)
        worker.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        token.cancel()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual([False], results)
        self.assertLess(time.monotonic() - started, 1)
