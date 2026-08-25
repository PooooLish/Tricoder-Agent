"""Tests for safe and exclusive eval output lifecycle."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tricoder.evals.output import (
    EvalOutputError,
    prepare_output_root,
    reserve_run_directory,
    validate_run_directory,
)


class EvalOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepare_output_root_rejects_linked_parent_before_child_creation(
        self,
    ) -> None:
        """Trusting a linked runtime/evals parent would permit redirected writes."""
        runtime = self.root / "runtime"
        runtime.mkdir()

        with patch(
            "tricoder.evals.output._is_link_or_reparse_point",
            side_effect=lambda path: path == runtime,
        ):
            with self.assertRaises(EvalOutputError):
                prepare_output_root(self.root)

        self.assertFalse((runtime / "evals").exists())

    def test_reserve_run_directory_never_reuses_a_colliding_directory(self) -> None:
        """Replacing exclusive mkdir with exist_ok would overwrite an older run."""
        first = reserve_run_directory(self.root, "run-fixed", max_attempts=2)
        marker = first / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")

        second = reserve_run_directory(self.root, "run-fixed", max_attempts=2)

        self.assertEqual(self.root / "runtime" / "evals" / "run-fixed-01", second)
        self.assertEqual("preserve", marker.read_text(encoding="utf-8"))
        self.assertEqual((marker,), tuple(first.iterdir()))
        self.assertEqual((), tuple(second.iterdir()))

    def test_validate_run_directory_rejects_nonempty_reserved_directory(self) -> None:
        """Runner must not append workspace/audit state to a nonempty reservation."""
        run_dir = reserve_run_directory(self.root, "run-fixed")
        (run_dir / "old-result.json").write_text("old", encoding="utf-8")

        with self.assertRaises(EvalOutputError):
            validate_run_directory(self.root, run_dir, require_empty=True)


if __name__ == "__main__":
    unittest.main()
