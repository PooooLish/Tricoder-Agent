import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from config import select
        self.assertEqual('red',select('red','red'))

