import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from renderer import render
        self.assertIsInstance(render('sample','blue'),str)

