import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from csv_records import parse_records
        self.assertEqual([['a','b'],['c','d']],parse_records('a,b\nc,d\n'))

    def test_boundary(self):
        from csv_records import parse_records
        self.assertEqual([],parse_records('')); self.assertEqual([['','x','']],parse_records(',x,\r\n')); self.assertEqual([[]],parse_records('\n'))

    def test_regression(self):
        from csv_records import parse_records
        self.assertEqual([['a,b','x"y'],['line\r\nbreak','z']],parse_records('"a,b","x""y"\r\n"line\r\nbreak",z\r\n'))

