import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from collection_ops import unique_stable
        self.assertEqual([8,2,5],unique_stable([8,2,8,5,2]))

    def test_boundary(self):
        from collection_ops import unique_stable
        self.assertEqual([],unique_stable([])); self.assertEqual([7],unique_stable([7,7,7]))

    def test_regression(self):
        from collection_ops import unique_stable
        x=[-1,0,-1,9]; self.assertEqual([-1,0,9],unique_stable(x)); self.assertEqual([-1,0,-1,9],x)

