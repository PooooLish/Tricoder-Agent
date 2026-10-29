import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from tasks import pending_once
        self.assertEqual(['b'],pending_once(['b'],set()))

