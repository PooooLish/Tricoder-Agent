import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from pricing import final_price
        from checkout import total
        self.assertAlmostEqual(40,final_price(80,50)); self.assertAlmostEqual(40,total(80,50))

    def test_boundary(self):
        from checkout import total
        self.assertEqual(0,total(12,100)); self.assertEqual(12,total(12,0)); self.assertEqual(0,total(0,20))

    def test_regression(self):
        from pricing import final_price
        from checkout import total
        self.assertAlmostEqual(7.3875,final_price(9.85,25)); self.assertAlmostEqual(7.3875,total(9.85,25))

