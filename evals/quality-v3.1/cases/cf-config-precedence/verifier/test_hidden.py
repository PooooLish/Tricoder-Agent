import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from settings import resolve
        from app import build_settings
        self.assertEqual({'x':3,'y':4},resolve({'x':1},{'x':2,'y':4},{'x':3})); self.assertEqual({'port':9000,'debug':False,'label':'file','token':None},build_settings({'label':'file'},{'port':9000}))

    def test_boundary(self):
        from settings import resolve
        from app import build_settings
        self.assertEqual({'a':0,'b':False,'c':'','d':None},resolve(dict.fromkeys('abcd',7),{},dict(a=0,b=False,c='',d=None)))
        self.assertEqual({'port':8000,'debug':False,'label':'app','token':None},build_settings({},{}))

    def test_regression(self):
        from settings import resolve
        from app import build_settings
        from copy import deepcopy
        a={'x':1}; b={'x':2}; c={'extra':3}; before=deepcopy((a,b,c)); self.assertEqual({'x':2,'extra':3},resolve(a,b,c)); self.assertEqual(before,(a,b,c))
        self.assertEqual(0,build_settings({'port':4},{'port':0})['port'])

