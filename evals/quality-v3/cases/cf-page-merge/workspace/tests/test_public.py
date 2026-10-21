import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from service import collect
        self.assertEqual([{'id':1}],collect([[{'id':1}],[{'id':1}]]))

