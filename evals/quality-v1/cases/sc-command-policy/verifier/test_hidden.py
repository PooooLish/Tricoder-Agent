import unittest
from commands import allowed

class HiddenTest(unittest.TestCase):
    def test_reject(self): self.assertFalse(allowed(['python','-m','unittest','&&','calc'])); self.assertFalse(allowed(['powershell']))
    def test_allow(self): self.assertTrue(allowed(['python','-m','unittest','-q']))
