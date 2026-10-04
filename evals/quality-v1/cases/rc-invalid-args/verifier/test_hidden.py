import unittest
from normalize import normalize

class HiddenTest(unittest.TestCase):
    def test_trim_lower(self): self.assertEqual('hello world', normalize('  Hello World  '))
    def test_empty(self): self.assertEqual('', normalize('   '))
