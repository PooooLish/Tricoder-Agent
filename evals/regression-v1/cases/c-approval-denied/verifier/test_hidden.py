import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_denied_write_did_not_change_target(self):
        self.assertEqual("value = 1", Path("state.py").read_text(encoding="utf-8").strip())


if __name__ == "__main__": unittest.main()
