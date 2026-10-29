import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from config import select
        self.assertEqual('green',select('red','green'))

    def test_boundary(self):
        from config import select
        self.assertEqual('',select('red','')); self.assertEqual('x',select('','x'))

    def test_regression(self):
        from config import select
        self.assertEqual('blue',select('orange','blue')); self.assertEqual(' ',select('red',' '))

