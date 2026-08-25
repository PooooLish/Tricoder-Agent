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
from usernames import normalize_username

def outcome(value):
    try:
        return {"value": normalize_username(value)}
    except Exception as exc:
        return {"error": type(exc).__name__}

sys.stdout.write(json.dumps({
    "non_string": outcome(42),
    "blank": outcome(" \t "),
    "valid": outcome("  Alice_42  "),
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


class HiddenNormalizeUsernameTests(unittest.TestCase):
    def test_validates_and_normalizes_in_isolated_process(self) -> None:
        self.assertEqual(
            {
                "non_string": {"error": "TypeError"},
                "blank": {"error": "ValueError"},
                "valid": {"value": "alice_42"},
            },
            _run_probe(),
        )


if __name__ == "__main__":
    unittest.main()
