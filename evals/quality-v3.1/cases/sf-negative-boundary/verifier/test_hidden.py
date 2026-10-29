import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from limits import non_negative
        self.assertEqual(6,non_negative(6))

    def test_boundary(self):
        from limits import non_negative
        self.assertEqual(0,non_negative(-9)); self.assertEqual(0,non_negative(0))

    def test_regression(self):
        from limits import non_negative
        self.assertEqual(10**25,non_negative(10**25)); self.assertEqual(0,non_negative(-10**25))

