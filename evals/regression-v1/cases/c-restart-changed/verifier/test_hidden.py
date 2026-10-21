import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_fixture_remains_bounded(self):
        self.assertIn(Path("app.py").read_text(encoding="utf-8").strip(), {"VALUE = 1", "VALUE = 2"})


if __name__ == "__main__": unittest.main()
