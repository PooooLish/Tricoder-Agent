import unittest
from parser import parse_number

class HiddenTest(unittest.TestCase):
    def test_spaces(self): self.assertEqual(42, parse_number(' 42 '))
    def test_negative(self): self.assertEqual(-3, parse_number('-3'))
