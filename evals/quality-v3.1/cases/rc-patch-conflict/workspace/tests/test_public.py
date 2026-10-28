import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from tags import replace_tag
        self.assertEqual('xx',replace_tag('aa','a','x'))

