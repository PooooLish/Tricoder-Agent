import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from formatter import render
        self.assertEqual('hello',render(' Hello ','P:'))

    def test_boundary(self):
        from formatter import render
        self.assertEqual('',render(' \t','x')); self.assertEqual('',render('',''))

    def test_regression(self):
        from formatter import render
        self.assertEqual('a b',render(' A B\n','Other')); self.assertEqual('mixed',render(' MiXeD ',''))

