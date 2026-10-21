import unittest
from stats_ops import mean

class HiddenTest(unittest.TestCase):
    def test_empty(self): self.assertEqual(0.0, mean([]))
    def test_values(self): self.assertEqual(2.5, mean([1.0, 4.0]))
