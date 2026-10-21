import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from sequence_ops import tail
        self.assertEqual([2,3],tail([1,2,3],2))

