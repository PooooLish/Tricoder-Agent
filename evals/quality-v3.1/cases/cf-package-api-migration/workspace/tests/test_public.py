import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from textkit import normalize_label,clean_label
        self.assertEqual('x',normalize_label(' X ')); self.assertEqual('x',clean_label(' X '))

