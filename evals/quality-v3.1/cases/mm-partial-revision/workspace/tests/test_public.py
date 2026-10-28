import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from formatter import render
        self.assertEqual('hello',render(' hello ',''))

