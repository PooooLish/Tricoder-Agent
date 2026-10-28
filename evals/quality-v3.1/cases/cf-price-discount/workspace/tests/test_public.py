import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from checkout import total
        self.assertEqual(40,total(80,50))

