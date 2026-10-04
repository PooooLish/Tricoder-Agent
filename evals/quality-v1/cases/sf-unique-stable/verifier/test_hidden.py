import unittest
from collection_ops import unique_stable

class HiddenTest(unittest.TestCase):
    def test_order(self): self.assertEqual([3, 1, 2], unique_stable([3, 1, 3, 2, 1]))
    def test_empty(self): self.assertEqual([], unique_stable([]))
