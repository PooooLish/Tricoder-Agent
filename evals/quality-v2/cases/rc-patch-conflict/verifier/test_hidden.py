import unittest
from tags import replace_tag

class HiddenTest(unittest.TestCase):
    def test_all(self): self.assertEqual('b b', replace_tag('a a', 'a', 'b'))
    def test_missing(self): self.assertEqual('x', replace_tag('x', 'a', 'b'))
