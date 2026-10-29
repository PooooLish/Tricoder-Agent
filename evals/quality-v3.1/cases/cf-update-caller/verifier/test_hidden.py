import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from report import product_line
        self.assertEqual('product=12',product_line(3,4))

    def test_boundary(self):
        from report import product_line
        self.assertEqual('product=0',product_line(0,4)); self.assertEqual('product=-12',product_line(-3,4))

    def test_regression(self):
        from report import product_line
        from calculator import multiply
        self.assertEqual(6,multiply(-2,-3)); self.assertEqual('product=6',product_line(-2,-3))

