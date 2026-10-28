import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from counter import increment
        self.assertEqual(6,increment(5))

    def test_boundary(self):
        from counter import increment
        self.assertEqual(-6,increment(-7)); self.assertEqual(1,increment(0))

    def test_regression(self):
        from counter import increment
        self.assertEqual(10**30+1,increment(10**30))

