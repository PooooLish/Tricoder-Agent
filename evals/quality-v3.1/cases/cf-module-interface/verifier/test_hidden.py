import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from api import display
        self.assertEqual('Ada Lovelace',display('ada','lovelace'))

    def test_boundary(self):
        from api import display
        self.assertEqual(' X',display('','x')); self.assertEqual(' ',display('',''))

    def test_regression(self):
        from api import display
        self.assertEqual('Mary Ann Smith',display('mARY aNN','sMITH'))

