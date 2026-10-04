import unittest
from tasks import pending_once

class HiddenTest(unittest.TestCase):
    def test_done_removed(self): self.assertEqual(['b'], pending_once(['a', 'b', 'a'], {'a'}))
    def test_stable_unique(self): self.assertEqual(['a', 'b'], pending_once(['a', 'a', 'b'], set()))
