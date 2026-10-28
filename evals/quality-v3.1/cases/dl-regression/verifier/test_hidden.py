import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from divide import divide
        self.assertEqual(3,divide(6,2))

    def test_boundary(self):
        from divide import divide
        self.assertIsNone(divide(3,0)); self.assertIsNone(divide(0,0)); self.assertEqual(0,divide(0,3))

    def test_regression(self):
        from divide import divide
        self.assertAlmostEqual(-2.5,divide(-5,2)); self.assertAlmostEqual(1.5,divide(3,2))

