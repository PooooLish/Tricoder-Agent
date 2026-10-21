import inspect
import unittest

from formatter import render


class HiddenTest(unittest.TestCase):
    def test_new_behavior_and_public_signature(self):
        self.assertEqual("Hi, Ada!", render("Ada"))
        self.assertEqual("Welcome, Ada!", render("Ada", "Welcome"))
        signature = inspect.signature(render)
        self.assertEqual(("name", "prefix"), tuple(signature.parameters))
        self.assertEqual("Hi", signature.parameters["prefix"].default)


if __name__ == "__main__":
    unittest.main()
