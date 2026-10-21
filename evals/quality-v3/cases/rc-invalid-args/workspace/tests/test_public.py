import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from normalize import normalize
        self.assertEqual('hello',normalize(' Hello '))

