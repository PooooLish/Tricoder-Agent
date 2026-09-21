import unittest

from smoke_demo import add, subtract


class TestAdd(unittest.TestCase):
    """测试 add 函数。"""

    def test_add_positive(self):
        """测试 add(2, 3) 等于 5。"""
        self.assertEqual(add(2, 3), 5)

    def test_add_opposite(self):
        """测试 add(-1, 1) 等于 0。"""
        self.assertEqual(add(-1, 1), 0)


class TestSubtract(unittest.TestCase):
    """测试 subtract 函数。"""

    def test_subtract(self):
        """测试 subtract(5, 3) 等于 2。"""
        self.assertEqual(subtract(5, 3), 2)

    def test_subtract_negative_result(self):
        """测试 subtract(1, 4) 等于 -3。"""
        self.assertEqual(subtract(1, 4), -3)


if __name__ == "__main__":
    unittest.main()
