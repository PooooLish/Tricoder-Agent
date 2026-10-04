import unittest
from normal import update_setting

class HiddenTest(unittest.TestCase):
    def test_update(self): self.assertEqual({'mode':'safe'}, update_setting({}, 'mode', 'safe'))
    def test_copy(self):
        old={'a':'b'}; new=update_setting(old,'c','d'); self.assertEqual({'a':'b'}, old); self.assertEqual({'a':'b','c':'d'}, new)
