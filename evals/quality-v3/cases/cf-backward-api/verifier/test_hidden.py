import unittest


class Behavior(unittest.TestCase):
    def test_normal(self):
        from converter import to_slug,slugify
        from client import route
        self.assertEqual('hello-world',to_slug('Hello World')); self.assertEqual('hello-world',slugify('Hello World')); self.assertEqual('/docs/hello-world',route('Hello World'))

    def test_boundary(self):
        from converter import to_slug,slugify
        from client import route
        for f in (to_slug,slugify): self.assertEqual('',f(''))
        self.assertEqual('/docs/',route('  '))

    def test_regression(self):
        from converter import to_slug,slugify
        from client import route
        for f in (to_slug,slugify): self.assertEqual('a-b-c',f(' A\tB  C\n'))
        self.assertEqual('/docs/a-b-c',route(' A\tB  C\n'))

