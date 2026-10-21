import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from pipeline import build_report
        self.assertEqual({'a':4},build_report('customer,amount,status\na,4,paid\n'))

