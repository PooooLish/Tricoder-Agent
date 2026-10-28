import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from sequence_ops import tail
        self.assertEqual([3,4], tail([1,2,3,4],2))

    def test_boundary(self):
        from sequence_ops import tail
        self.assertEqual([],tail([1],0)); self.assertEqual([2,1],tail([2,1],9)); self.assertEqual([],tail([],2))

    def test_regression(self):
        from sequence_ops import tail
        x=[7,2,5]; self.assertEqual([2,5],tail(x,2)); self.assertEqual([7,2,5],x)

