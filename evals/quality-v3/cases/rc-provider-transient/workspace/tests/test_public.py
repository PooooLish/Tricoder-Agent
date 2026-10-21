import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from counter import increment
        self.assertEqual(2,increment(1))

