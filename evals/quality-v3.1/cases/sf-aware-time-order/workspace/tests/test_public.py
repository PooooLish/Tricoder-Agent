import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from time_order import sort_events
        self.assertEqual([],sort_events([]))

