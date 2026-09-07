"""Real-process regression coverage for bounded process-tree cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.subprocess_control import run_bounded_process


class BoundedProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cancellation_terminates_running_process_before_side_effect(self) -> None:
        """取消命令时必须终止受管进程树，并以主动取消而非超时结束。"""
        marker = self.root / "cancelled-process-survived.txt"
        script = self.root / "cancellable.py"
        script.write_text(
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(1.0)\n"
            f"Path({str(marker)!r}).write_text('alive')\n",
            encoding="utf-8",
        )
        token = CancellationToken()
        timer = threading.Timer(0.15, token.cancel)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(CancellationError):
                run_bounded_process(
                    [sys.executable, str(script)],
                    cwd=self.root,
                    env=dict(os.environ),
                    timeout=5,
                    max_output_bytes=4096,
                    cancellation=token,
                )
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        time.sleep(1.1)

        self.assertLess(elapsed, 1.0)
        self.assertFalse(marker.exists())

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

    def test_cap_plus_one_terminates_tree_before_delayed_marker(self) -> None:
        """刚超过上限的少量输出也必须立即触发进程树清理。"""

        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream=stream_name):
                marker = self.root / f"{stream_name}-after-overflow.txt"
                script = self.root / f"{stream_name}_overflow.py"
                script.write_text(
                    "import sys, time\n"
                    "from pathlib import Path\n"
                    f"stream = sys.{stream_name}.buffer\n"
                    "stream.write(b'x' * 4097)\n"
                    "stream.flush()\n"
                    "time.sleep(0.8)\n"
                    f"Path({str(marker)!r}).write_text('alive')\n"
                    "time.sleep(10)\n",
                    encoding="utf-8",
                )

                started = time.monotonic()
                result = run_bounded_process(
                    [sys.executable, str(script)],
                    cwd=self.root,
                    env=dict(os.environ),
                    timeout=3,
                    max_output_bytes=4096,
                )
                elapsed = time.monotonic() - started
                time.sleep(1.0)

                self.assertTrue(result.output_exceeded)
                self.assertFalse(result.timed_out)
                self.assertLess(elapsed, 2.0)
                self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
