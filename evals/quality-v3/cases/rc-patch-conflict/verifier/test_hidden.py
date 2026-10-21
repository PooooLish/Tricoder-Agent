import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from tags import replace_tag
        self.assertEqual('bb',replace_tag('aa','a','b'))

    def test_boundary(self):
        from tags import replace_tag
        self.assertEqual('',replace_tag('aaa','a','')); self.assertEqual('',replace_tag('','x','y'))

    def test_regression(self):
        from tags import replace_tag
        self.assertEqual(' x ',replace_tag(' x ','z','q')); self.assertEqual(' ab ab ',replace_tag(' zz zz ','zz','ab'))

