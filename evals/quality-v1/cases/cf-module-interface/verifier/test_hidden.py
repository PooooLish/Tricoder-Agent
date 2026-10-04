import unittest
from api import display

class HiddenTest(unittest.TestCase):
    def test_name(self): self.assertEqual('Ada Lovelace', display('ada', 'lovelace'))
    def test_spacing(self): self.assertEqual('Li Lei', display('li', 'lei'))
