import unittest
from safe_guard import may_read

class HiddenTest(unittest.TestCase):
    def test_sensitive(self): self.assertFalse(may_read('.env.local')); self.assertFalse(may_read('x/credentials.json'))
    def test_normal(self): self.assertTrue(may_read('README.md'))
