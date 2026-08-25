"""Tests for the shared subprocess environment filter."""

from __future__ import annotations

import unittest

from tricoder.subprocess_env import filtered_subprocess_env


class SubprocessEnvironmentTests(unittest.TestCase):
    def test_filtered_subprocess_env_removes_credentials(self) -> None:
        source = {
            "PATH": "safe",
            "OPENAI_API_KEY": "secret",
            "CUSTOM_TOKEN": "secret",
            "PGPASSWORD": "secret",
            "MYAPP_SECRET": "secret",
            "AWS_ACCESS_KEY": "secret",
            "AUTHORIZATION": "secret",
            "ORDINARY_SETTING": "visible",
            "PYTHONDONTWRITEBYTECODE": "0",
            "PYTEST_ADDOPTS": "--pdb",
        }

        env = filtered_subprocess_env(source)

        self.assertEqual("safe", env["PATH"])
        self.assertEqual("visible", env["ORDINARY_SETTING"])
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CUSTOM_TOKEN", env)
        self.assertNotIn("PGPASSWORD", env)
        self.assertNotIn("MYAPP_SECRET", env)
        self.assertNotIn("AWS_ACCESS_KEY", env)
        self.assertNotIn("AUTHORIZATION", env)
        self.assertIn("CUSTOM_TOKEN", source)
        self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual("-p no:cacheprovider", env["PYTEST_ADDOPTS"])


if __name__ == "__main__":
    unittest.main()
