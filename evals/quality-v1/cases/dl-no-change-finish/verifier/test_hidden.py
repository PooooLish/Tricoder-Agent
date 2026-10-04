import unittest
from health import healthy

class HiddenTest(unittest.TestCase):
    def test_healthy(self): self.assertTrue(healthy())
