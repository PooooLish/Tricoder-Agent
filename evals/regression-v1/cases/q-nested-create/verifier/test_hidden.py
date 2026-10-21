import subprocess
import sys
import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_nested_program_output(self):
        target = Path("app/demo/main.py")
        self.assertTrue(target.is_file())
        completed = subprocess.run(
            [sys.executable, str(target)], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, completed.returncode)
        self.assertEqual("Hello, world!", completed.stdout.strip())
        self.assertEqual("", completed.stderr)


if __name__ == "__main__":
    unittest.main()
