import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from searcher import find_prefix
        self.assertEqual(['Alpha'],find_prefix(['Alpha','Beta'],'a'))

