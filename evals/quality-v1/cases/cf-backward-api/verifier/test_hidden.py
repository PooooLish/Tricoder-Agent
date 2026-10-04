import unittest
from client import route
from converter import to_slug, slugify

class HiddenTest(unittest.TestCase):
    def test_old_api(self): self.assertEqual('/docs/hello-world', route('Hello World'))
    def test_new_api(self): self.assertEqual('a-b', to_slug('A B'))
    def test_alias(self): self.assertEqual(to_slug('Keep API'), slugify('Keep API'))
