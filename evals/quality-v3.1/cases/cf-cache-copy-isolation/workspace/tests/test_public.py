import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from cache import RecordCache
        from service import load_record
        c=RecordCache(); c.put('a',{'x':1}); self.assertEqual({'x':1},load_record(c,'a'))

