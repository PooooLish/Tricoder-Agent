import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from api import display
        self.assertEqual('Ada Lovelace',display('ada','lovelace'))

