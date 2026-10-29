import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from math_ops import subtract
        self.assertEqual(5, subtract(8,3))

    def test_boundary(self):
        from math_ops import subtract
        self.assertEqual(-9, subtract(-4,5)); self.assertEqual(0, subtract(0,0))

    def test_regression(self):
        from math_ops import subtract
        self.assertEqual(0, subtract(-7,-7)); self.assertEqual(1, subtract(10**30+1,10**30))

