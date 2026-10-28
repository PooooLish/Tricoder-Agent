import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from stats_ops import mean
        self.assertAlmostEqual(3,mean([2,4]))

    def test_boundary(self):
        from stats_ops import mean
        self.assertEqual(0.0,mean([])); self.assertAlmostEqual(-2.5,mean([-2.5]))

    def test_regression(self):
        from stats_ops import mean
        x=[-2,1,2.5]; self.assertAlmostEqual(.5,mean(x)); self.assertEqual([-2,1,2.5],x)

