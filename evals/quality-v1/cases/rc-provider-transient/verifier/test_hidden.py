import unittest
from counter import increment

class HiddenTest(unittest.TestCase):
    def test_positive(self): self.assertEqual(5, increment(4))
    def test_negative(self): self.assertEqual(0, increment(-1))
