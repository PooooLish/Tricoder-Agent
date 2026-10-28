import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from math_ops import subtract
        self.assertEqual(3, subtract(5,2))

