import unittest
from limits import non_negative

class HiddenTest(unittest.TestCase):
    def test_negative(self): self.assertEqual(0, non_negative(-5))
    def test_zero(self): self.assertEqual(0, non_negative(0))
    def test_positive(self): self.assertEqual(7, non_negative(7))
