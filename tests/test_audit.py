import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.audit import AuditLogger, redact


class AuditTests(unittest.TestCase):
    def test_redacts_nested_secret_fields_without_losing_other_context(self) -> None:
        """防止嵌套凭据进入轨迹，同时保留可审计的非敏感字段。"""
        event = {
            "tool": "provider",
            "arguments": {
                "api_key": "secret-a",
                "nested": {"Authorization": "Bearer secret-b", "path": "src/app.py"},
            },
        }

        cleaned = redact(event)

        self.assertEqual("***", cleaned["arguments"]["api_key"])
        self.assertEqual("***", cleaned["arguments"]["nested"]["Authorization"])
        self.assertEqual("src/app.py", cleaned["arguments"]["nested"]["path"])

    def test_logger_writes_one_valid_json_object_per_line(self) -> None:
        """防止轨迹格式损坏或凭据以明文写盘。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            logger = AuditLogger(path)

            logger.log({"status": "ok", "token": "secret", "duration_ms": 12})

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            decoded = json.loads(lines[0])
            self.assertEqual("***", decoded["token"])
            self.assertEqual("ok", decoded["status"])
            self.assertIn("timestamp", decoded)

    def test_logger_replaces_patch_source_with_character_count(self) -> None:
        """防止 unified diff 或源码哨兵进入持久化 JSONL。"""
        sentinel = "PATCH-PRIVATE-SOURCE-SENTINEL-4E8F"
        patch_text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            f"-{sentinel}\n"
            "+safe = True\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patch.jsonl"
            logger = AuditLogger(path)

            logger.log({"tool": "apply_patch", "arguments": {"patch": patch_text}})

            serialized = path.read_text(encoding="utf-8")
            event = json.loads(serialized)
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn("patch", event["arguments"])
            self.assertEqual(len(patch_text), event["arguments"]["patch_chars"])

    def test_logger_preflight_converts_directory_creation_failure_to_safe_error(self) -> None:
        """防止审计目录不可创建时泄露底层路径或延迟到运行期才失败。"""
        with tempfile.TemporaryDirectory() as directory:
            logger = AuditLogger(Path(directory) / "blocked" / "run.jsonl")
            prepare = getattr(logger, "prepare", None)
            self.assertIsNotNone(prepare, "AuditLogger 缺少写入预检")

            with patch.object(
                Path,
                "mkdir",
                side_effect=PermissionError("DIRECTORY-SENTINEL"),
            ):
                with self.assertRaises(OSError) as captured:
                    prepare()

            self.assertIn("审计日志", str(captured.exception))
            self.assertNotIn("DIRECTORY-SENTINEL", str(captured.exception))

    def test_logger_converts_append_failure_to_safe_error(self) -> None:
        """防止后续 JSONL 追加失败时抛出包含底层自由文本的异常。"""
        with tempfile.TemporaryDirectory() as directory:
            logger = AuditLogger(Path(directory) / "run.jsonl")

            with patch.object(
                Path,
                "open",
                side_effect=PermissionError("APPEND-SENTINEL"),
            ):
                with self.assertRaises(OSError) as captured:
                    logger.log({"status": "ok"})

            self.assertIn("审计日志", str(captured.exception))
            self.assertNotIn("APPEND-SENTINEL", str(captured.exception))

if __name__ == "__main__":
    unittest.main()
