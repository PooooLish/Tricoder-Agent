import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from tasks import pending_once
        self.assertEqual(['b','c'],pending_once(['a','b','b','c'],{'a'}))

    def test_boundary(self):
        from tasks import pending_once
        self.assertEqual([],pending_once([],set())); self.assertEqual([],pending_once(['a','a'],{'a'}))

    def test_regression(self):
        from tasks import pending_once
        x=['z','b','z','a']; done={'a'}; self.assertEqual(['z','b'],pending_once(x,done)); self.assertEqual(['z','b','z','a'],x); self.assertEqual({'a'},done)

