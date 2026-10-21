import unittest


class Behavior(unittest.TestCase):
    def test_public(self):
        from jsonl_reader import parse_lines
        self.assertEqual(([{'a':1}],[]),parse_lines('{"a":1}\n'))

