import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from service import accept
        self.assertEqual('invalid',accept(0)); self.assertEqual('ok',accept(1))

