import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from parser import parse_number
        self.assertEqual(-12,parse_number(' -12 '))

