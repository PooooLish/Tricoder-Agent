import unittest
from service import accept

class HiddenTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual('invalid', accept(0)); self.assertEqual('ok', accept(1)); self.assertEqual('ok', accept(100)); self.assertEqual('invalid', accept(101))
