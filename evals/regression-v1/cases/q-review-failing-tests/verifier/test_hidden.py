import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_review_left_known_bug_unchanged(self):
        text = Path("calculator.py").read_text(encoding="utf-8")
        self.assertIn("return b - a", text)
        self.assertIn("reversed_operands", text)


if __name__ == "__main__":
    unittest.main()
