import sys
import threading
import unittest
from pathlib import Path


# 让 ``python -m unittest`` 在未安装包的源码工作树中也能直接发现 ``src``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.core.budgets import BudgetExceeded, ExecutionBudget


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ExecutionBudgetTests(unittest.TestCase):
    def test_consumes_round_token_and_subtask_limits_at_the_boundary(self) -> None:
        """防止预算在恰好用尽时被提前拒绝，或允许继续透支。"""
        budget = ExecutionBudget(
            max_rounds=2,
            max_tokens=10,
            timeout_seconds=30,
            max_subtasks=1,
        )

        budget.consume(rounds=2, tokens=10, subtasks=1)

        self.assertEqual(0, budget.remaining_rounds)
        self.assertEqual(0, budget.remaining_tokens)
        self.assertEqual(0, budget.remaining_subtasks)
        for request in ({"rounds": 1}, {"tokens": 1}, {"subtasks": 1}):
            with self.subTest(request=request):
                with self.assertRaises(BudgetExceeded):
                    budget.consume(**request)

    def test_rejected_multi_dimension_consumption_is_atomic(self) -> None:
        """防止某一维预算不足时，其他维度仍被部分扣减。"""
        budget = ExecutionBudget(2, 5, 30, 1)

        with self.assertRaises(BudgetExceeded) as raised:
            budget.consume(rounds=1, tokens=6)

        self.assertEqual("tokens", raised.exception.resource)
        self.assertEqual(2, budget.remaining_rounds)
        self.assertEqual(5, budget.remaining_tokens)

    def test_monotonic_deadline_rejects_work_at_timeout(self) -> None:
        """防止到达时间硬上限后仍可继续消费其他预算。"""
        clock = _FakeClock()
        budget = ExecutionBudget(2, 10, 5, 1, clock=clock)
        self.assertEqual(5, budget.remaining_seconds)

        clock.now = 105.0

        self.assertEqual(0, budget.remaining_seconds)
        with self.assertRaises(BudgetExceeded) as raised:
            budget.raise_if_exceeded()
        self.assertEqual("time", raised.exception.resource)

    def test_invalid_limits_and_consumption_are_rejected(self) -> None:
        """防止负数、布尔值或非数值输入扩大可用预算。"""
        invalid_limits = (
            {"max_rounds": -1, "max_tokens": 1, "timeout_seconds": 1, "max_subtasks": 1},
            {"max_rounds": 1, "max_tokens": True, "timeout_seconds": 1, "max_subtasks": 1},
            {"max_rounds": 1, "max_tokens": 1, "timeout_seconds": -1, "max_subtasks": 1},
        )
        for values in invalid_limits:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ExecutionBudget(**values)

        budget = ExecutionBudget(1, 1, 1, 1)
        for request in ({"rounds": -1}, {"tokens": True}, {"subtasks": 1.5}):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    budget.consume(**request)

    def test_concurrent_consumers_cannot_make_budget_negative(self) -> None:
        """防止并发检查与扣减分离而让轮次预算出现负数。"""
        budget = ExecutionBudget(5, 100, 30, 10)
        barrier = threading.Barrier(20)
        outcomes: list[bool] = []
        outcomes_lock = threading.Lock()

        def consume_one() -> None:
            barrier.wait()
            try:
                budget.consume(rounds=1)
                succeeded = True
            except BudgetExceeded:
                succeeded = False
            with outcomes_lock:
                outcomes.append(succeeded)

        workers = [threading.Thread(target=consume_one) for _ in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(5, sum(outcomes))
        self.assertEqual(0, budget.remaining_rounds)


if __name__ == "__main__":
    unittest.main()
