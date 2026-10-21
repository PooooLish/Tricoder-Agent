import unittest
from config import select

class HiddenTest(unittest.TestCase):
    def test_latest(self): self.assertEqual('blue', select('red', 'blue'))
    def test_empty_latest(self): self.assertEqual('', select('old', ''))
