import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


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
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _PROBE],
        cwd=Path.cwd(),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) > 4096:
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
