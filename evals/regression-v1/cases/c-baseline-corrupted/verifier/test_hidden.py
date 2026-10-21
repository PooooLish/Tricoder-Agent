import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_source_unchanged(self):
        self.assertEqual("VALUE = 1", Path("app.py").read_text(encoding="utf-8").strip())


if __name__ == "__main__": unittest.main()
