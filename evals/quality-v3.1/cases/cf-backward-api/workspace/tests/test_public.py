import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from converter import to_slug,slugify
        self.assertEqual(to_slug('Hello World'),slugify('Hello World'))

