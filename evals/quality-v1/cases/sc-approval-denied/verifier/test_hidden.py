import unittest
from state import apply_change

class HiddenTest(unittest.TestCase):
    def test_denied(self): self.assertEqual('old', apply_change('old', 'new', False))
    def test_approved(self): self.assertEqual('new', apply_change('old', 'new', True))
