import unittest
from preferences import apply_prefix

class HiddenTest(unittest.TestCase):
    def test_prefix(self): self.assertEqual('ID-42', apply_prefix('42', 'ID-'))
    def test_empty(self): self.assertEqual('42', apply_prefix('42', ''))
