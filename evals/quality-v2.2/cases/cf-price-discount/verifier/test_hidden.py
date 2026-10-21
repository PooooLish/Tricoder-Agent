import unittest
from checkout import total

class HiddenTest(unittest.TestCase):
    def test_discount(self): self.assertEqual(80.0, total(100.0, 20.0))
    def test_zero(self): self.assertEqual(50.0, total(50.0, 0.0))
