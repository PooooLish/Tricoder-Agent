import unittest
from math_ops import subtract

class HiddenTest(unittest.TestCase):
    def test_positive(self): self.assertEqual(5, subtract(9, 4))
    def test_negative(self): self.assertEqual(4, subtract(-3, -7))
