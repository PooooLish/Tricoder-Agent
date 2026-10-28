import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from collection_ops import unique_stable
        self.assertEqual([3,1],unique_stable([3,1,3]))

