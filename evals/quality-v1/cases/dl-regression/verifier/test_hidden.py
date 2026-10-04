import unittest
from divide import divide

class HiddenTest(unittest.TestCase):
    def test_zero(self): self.assertIsNone(divide(4,0))
    def test_regular(self): self.assertEqual(2.5, divide(5,2))
