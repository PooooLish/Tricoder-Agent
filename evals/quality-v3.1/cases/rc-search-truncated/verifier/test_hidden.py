import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from searcher import find_prefix
        self.assertEqual(['Alpha','aB'],find_prefix(['Alpha','Beta','aB'],'A'))

    def test_boundary(self):
        from searcher import find_prefix
        self.assertEqual([],find_prefix([],'a')); self.assertEqual(['X','x'],find_prefix(['X','x'],''))

    def test_regression(self):
        from searcher import find_prefix
        self.assertEqual(['Ab','AB','Ab'],find_prefix(['Ab','z','AB','Ab'],'ab'))

