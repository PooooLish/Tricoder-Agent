import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_workspace_unchanged(self):
        self.assertEqual("unchanged", Path("marker.txt").read_text(encoding="utf-8").strip())


if __name__ == "__main__": unittest.main()
