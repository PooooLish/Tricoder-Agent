import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from parser import parse_number
        self.assertEqual(27,parse_number('27'))

    def test_boundary(self):
        from parser import parse_number
        self.assertEqual(-19,parse_number(' -19\n')); self.assertEqual(0,parse_number('+0'))

    def test_regression(self):
        from parser import parse_number
        self.assertEqual(10**30+7,parse_number(str(10**30+7))); self.assertEqual(3,parse_number(' +003 '))

