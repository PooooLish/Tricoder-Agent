import unittest
from feature import enabled

class HiddenTest(unittest.TestCase):
    def test_true(self): self.assertTrue(enabled(True))
    def test_false(self): self.assertFalse(enabled(False))
