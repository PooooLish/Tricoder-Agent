import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from loader import load_records
        self.assertEqual([{'id':'1','name':'A'}],load_records('[{"id":"1","full_name":"A"}]'))

