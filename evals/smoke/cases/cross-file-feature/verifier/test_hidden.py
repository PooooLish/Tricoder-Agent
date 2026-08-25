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
from discounts import percentage_discount
from pricing import final_price

def outcome(function, *args):
    try:
        return {"value": function(*args)}
    except Exception as exc:
        return {"error": type(exc).__name__}

sys.stdout.write(json.dumps({
    "discount": outcome(percentage_discount, 100, 25),
    "bad_amount": outcome(percentage_discount, -1, 10),
    "bad_percent_low": outcome(percentage_discount, 100, -1),
    "bad_percent_high": outcome(percentage_discount, 100, 101),
    "default_price": outcome(final_price, 80),
    "discounted_price": outcome(final_price, 80, 25),
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


class HiddenDiscountTests(unittest.TestCase):
    def test_discount_contract_in_isolated_process(self) -> None:
        self.assertEqual(
            {
                "discount": {"value": 75},
                "bad_amount": {"error": "ValueError"},
                "bad_percent_low": {"error": "ValueError"},
                "bad_percent_high": {"error": "ValueError"},
                "default_price": {"value": 80},
                "discounted_price": {"value": 60},
            },
            _run_probe(),
        )


if __name__ == "__main__":
    unittest.main()
