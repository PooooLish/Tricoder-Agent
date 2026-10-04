import unittest
from searcher import find_prefix

class HiddenTest(unittest.TestCase):
    def test_casefold(self): self.assertEqual(['Alpha', 'apple'], find_prefix(['Alpha', 'beta', 'apple'], 'a'))
    def test_none(self): self.assertEqual([], find_prefix(['beta'], 'a'))
