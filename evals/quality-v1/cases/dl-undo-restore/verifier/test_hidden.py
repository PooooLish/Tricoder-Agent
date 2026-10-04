import unittest
import state

class HiddenTest(unittest.TestCase):
    def test_restored(self): self.assertEqual('original', state.VALUE)
