import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_tail_marker_is_outside_initial_prefix(self):
        payload = Path("logs/failure.log").read_text(encoding="utf-8")
        marker = payload.index("EVAL_TAIL_731")
        self.assertGreater(marker, 20000)
        self.assertIn("connection_pool_exhausted", payload[marker:])


if __name__ == "__main__":
    unittest.main()
