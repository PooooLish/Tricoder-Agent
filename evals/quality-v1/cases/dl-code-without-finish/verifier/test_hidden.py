import unittest
from math_ops import add

class HiddenTest(unittest.TestCase):
    def test_positive(self): self.assertEqual(7, add(3,4))
    def test_negative(self): self.assertEqual(-5, add(-2,-3))
