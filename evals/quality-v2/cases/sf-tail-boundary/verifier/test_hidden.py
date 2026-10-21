import unittest
from sequence_ops import tail

class HiddenTest(unittest.TestCase):
    def test_zero(self): self.assertEqual([], tail([1, 2, 3], 0))
    def test_regular(self): self.assertEqual([2, 3], tail([1, 2, 3], 2))
