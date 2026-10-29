import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from cache import RecordCache
        from service import load_record
        c=RecordCache(); c.put('a',{'x':1}); c.put('b',{'x':2}); self.assertEqual({'x':1},load_record(c,'a')); self.assertEqual({'x':2},c.get('b'))

    def test_boundary(self):
        from cache import RecordCache
        from service import load_record
        c=RecordCache()
        with self.assertRaises(KeyError): c.get('missing')
        with self.assertRaises(KeyError): load_record(c,'missing')
        c.put('',{}); self.assertEqual({},c.get(''))

    def test_regression(self):
        from cache import RecordCache
        from service import load_record
        c=RecordCache(); value={'x':[{'y':1}]}; c.put('a',value); c.put('b',value); value['x'][0]['y']=2
        self.assertEqual({'x':[{'y':1}]},c.get('a')); result=load_record(c,'a'); result['x'][0]['y']=3
        self.assertEqual({'x':[{'y':1}]},load_record(c,'a')); self.assertEqual({'x':[{'y':1}]},c.get('b'))
        from unittest.mock import patch
        with patch.object(RecordCache, 'get', autospec=True, side_effect=RecordCache.get) as observed:
            self.assertEqual({'x':[{'y':1}]},load_record(c,'a'))
            observed.assert_any_call(c, 'a')
