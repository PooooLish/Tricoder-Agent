import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from stats_ops import mean
        self.assertEqual(3,mean([2,4]))

