import json
import os
from pathlib import Path
import sys
import unittest

from _tricoder_bounded_process import run_bounded_process


_PROBE = r"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))
from calculator import subtract

sys.stdout.write(json.dumps({
    "positive": subtract(7, 2),
    "negative": subtract(-3, -2),
}, separators=(",", ":")))
"""


def _run_probe() -> object:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = run_bounded_process(
        [sys.executable, "-I", "-B", "-c", _PROBE],
        cwd=Path.cwd(),
        env=env,
        timeout=5,
        max_output_bytes=4096,
    )
    if (
        completed.returncode != 0
        or completed.timed_out
        or completed.output_exceeded
        or completed.cleanup_failed
    ):
        raise AssertionError("isolated business probe failed")
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise AssertionError("isolated business probe returned invalid JSON") from exc


class HiddenSubtractTests(unittest.TestCase):
    def test_subtracts_expected_values_in_isolated_process(self) -> None:
        self.assertEqual({"positive": 5, "negative": -1}, _run_probe())


if __name__ == "__main__":
    unittest.main()
