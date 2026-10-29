import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from validator import valid_quantity
        from service import accept
        self.assertTrue(valid_quantity(50)); self.assertEqual('ok',accept(50))

    def test_boundary(self):
        from validator import valid_quantity
        from service import accept
        for n in (1,100):
            self.assertTrue(valid_quantity(n)); self.assertEqual('ok',accept(n))

    def test_regression(self):
        from validator import valid_quantity
        from service import accept
        for n in (-3,0,101,200):
            self.assertFalse(valid_quantity(n)); self.assertEqual('invalid',accept(n))

