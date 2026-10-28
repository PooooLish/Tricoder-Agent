import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from records import normalize
        from loader import load_records
        self.assertEqual({'id':'a','name':'Ada'},normalize({'id':'a','full_name':'Ada','other':'x'}))
        self.assertEqual([{'id':'a','name':'A'},{'id':'b','name':'B'}],load_records('[{"id":"a","full_name":"A"},{"id":"b","name":"B"}]'))

    def test_boundary(self):
        from records import normalize
        from loader import load_records
        self.assertEqual({'id':'','name':''},normalize({'id':'','name':'','full_name':'fallback'})); self.assertEqual([],load_records('[]'))
        with self.assertRaises(ValueError): normalize({'id':'x'})
        with self.assertRaises(ValueError): load_records('[{"id":"x"}]')

    def test_regression(self):
        from records import normalize
        x={'id':'007','name':'New','full_name':'Old','extra':'keep'}; before=x.copy()
        self.assertEqual({'id':'007','name':'New'},normalize(x)); self.assertEqual(before,x)

