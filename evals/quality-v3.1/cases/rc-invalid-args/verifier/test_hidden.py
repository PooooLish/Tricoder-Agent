import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from normalize import normalize
        self.assertEqual('hello',normalize(' Hello '))

    def test_boundary(self):
        from normalize import normalize
        self.assertEqual('',normalize(' \t\n')); self.assertEqual('',normalize(''))

    def test_regression(self):
        from normalize import normalize
        self.assertEqual('a  b\tc',normalize(' A  B\tC\n'))

