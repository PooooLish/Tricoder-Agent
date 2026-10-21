import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from limits import non_negative
        self.assertEqual(0,non_negative(-2))

