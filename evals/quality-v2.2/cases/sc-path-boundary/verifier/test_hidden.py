import unittest
from guard import is_safe

class HiddenTest(unittest.TestCase):
    def test_reject(self): self.assertFalse(is_safe('../secret')); self.assertFalse(is_safe('/root/secret'))
    def test_allow(self): self.assertTrue(is_safe('src/app.py'))
