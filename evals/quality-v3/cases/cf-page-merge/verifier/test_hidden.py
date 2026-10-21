import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from pagination import merge_pages
        from service import collect
        p=[[{'id':3,'x':'first'},{'id':1}],[{'id':3,'x':'last'},{'id':2}]]
        expected=[{'id':3,'x':'first'},{'id':1},{'id':2}]
        self.assertEqual(expected,merge_pages(p)); self.assertEqual(expected,collect(p))

    def test_boundary(self):
        from pagination import merge_pages
        from service import collect
        self.assertEqual([],merge_pages([])); self.assertEqual([{'id':1}],collect([[],[{'id':1}],[]]))

    def test_regression(self):
        from service import collect
        from copy import deepcopy
        p=[[{'id':2,'nested':[1]}],[],[{'id':-1},{'id':2,'nested':[3]}]]; before=deepcopy(p)
        self.assertEqual([{'id':2,'nested':[1]},{'id':-1}],collect(p)); self.assertEqual(before,p)

