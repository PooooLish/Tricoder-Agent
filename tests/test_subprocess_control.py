"""Real-process regression coverage for bounded process-tree cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from tricoder.subprocess_control import run_bounded_process


class BoundedProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_normal_parent_exit_cleans_delayed_descendant(self) -> None:
        """A zero-exit session leader must not orphan a delayed child."""
        child_code = (
            "import time; from pathlib import Path; "
            "time.sleep(0.6); Path('normal-child-alive.txt').write_text('alive')"
        )
        parent = self.root / "normal_parent.py"
        parent.write_text(
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL)\n",
            encoding="utf-8",
        )

        result = run_bounded_process(
            [sys.executable, str(parent)],
            cwd=self.root,
            env=dict(os.environ),
            timeout=5,
            max_output_bytes=4096,
        )
        time.sleep(0.9)

        self.assertEqual(0, result.returncode)
        self.assertFalse(result.cleanup_failed)
        self.assertFalse((self.root / "normal-child-alive.txt").exists())


if __name__ == "__main__":
    unittest.main()
