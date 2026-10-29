import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from divide import divide
        self.assertEqual(3,divide(6,2)); self.assertIsNone(divide(2,0))

