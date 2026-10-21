import unittest
from sessions import value_for

class HiddenTest(unittest.TestCase):
    def test_isolation(self): self.assertEqual('B', value_for({'a': 'A', 'b': 'B'}, 'b'))
    def test_first(self): self.assertEqual('A', value_for({'a': 'A', 'b': 'B'}, 'a'))
