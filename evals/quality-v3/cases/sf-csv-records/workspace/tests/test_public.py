import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from csv_records import parse_records
        self.assertEqual([['a,b','c']],parse_records('"a,b",c\n'))

