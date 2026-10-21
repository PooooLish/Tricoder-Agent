import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from report import product_line
        self.assertEqual('product=6',product_line(2,3))

