import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from app import build_settings
        self.assertEqual(9000,build_settings({}, {'port':9000})['port'])

