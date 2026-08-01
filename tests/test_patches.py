"""受限 unified diff 解析与内存应用的行为测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.patches import PatchError, apply_file_patch, parse_unified_diff


UPDATE_PATCH = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 value = 1
-enabled = False
+enabled = True
"""

CREATE_PATCH = """--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+value = 1
+print(value)
"""

NO_NEWLINE_PATCH = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
\\ No newline at end of file
+value = 2
\\ No newline at end of file
"""

INVALID_PATCHES = {
    "delete": "--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-value = 1\n",
    "rename": "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-old\n+new\n",
    "binary": "Binary files a/a.png and b/a.png differ\n",
    "overlap": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n@@ -1 +1 @@\n-b\n+c\n",
    "bad_count": "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1 @@\n-a\n+b\n",
    "duplicate": UPDATE_PATCH + UPDATE_PATCH,
}

INVALID_PATH_PATCHES = {
    "traversal": "--- a/dir/../app.py\n+++ b/dir/../app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "absolute_posix": "--- a//tmp/app.py\n+++ b//tmp/app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "absolute_windows": "--- a/C:/tmp/app.py\n+++ b/C:/tmp/app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "backslash": "--- a/dir\\app.py\n+++ b/dir\\app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "empty_segment": "--- a/dir//app.py\n+++ b/dir//app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "dot_segment": "--- a/./app.py\n+++ b/./app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "missing_prefix": "--- app.py\n+++ app.py\n@@ -1 +1 @@\n-old\n+new\n",
    "alias_duplicate": (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        "--- a/dir/../app.py\n+++ b/dir/../app.py\n@@ -1 +1 @@\n-old\n+new\n"
    ),
}


class PatchParserTests(unittest.TestCase):
    def test_parses_and_applies_exact_update_hunk(self) -> None:
        patch = parse_unified_diff(UPDATE_PATCH)[0]
        self.assertEqual("app.py", patch.path)
        self.assertFalse(patch.create)
        self.assertEqual(
            "value = 1\nenabled = True\n",
            apply_file_patch("value = 1\nenabled = False\n", patch),
        )

    def test_parses_new_file_from_dev_null(self) -> None:
        patch = parse_unified_diff(CREATE_PATCH)[0]
        self.assertTrue(patch.create)
        self.assertEqual("value = 1\nprint(value)\n", apply_file_patch("", patch))

    def test_preserves_absent_final_newline_marked_by_hunk(self) -> None:
        """防止换行标记被当作内容，或错误添加末尾换行。"""
        patch = parse_unified_diff(NO_NEWLINE_PATCH)[0]

        self.assertEqual("value = 2", apply_file_patch("value = 1", patch))

    def test_rejects_disallowed_or_malformed_patch_forms(self) -> None:
        """防止删除、改名和畸形 hunk 绕过受限语法。"""
        for name, source in INVALID_PATCHES.items():
            with self.subTest(name=name):
                with self.assertRaises(PatchError):
                    parse_unified_diff(source)

    def test_rejects_noncanonical_or_aliasing_patch_paths(self) -> None:
        """防止路径别名绕过同一目标的重复补丁拒绝。"""
        for name, source in INVALID_PATH_PATCHES.items():
            with self.subTest(name=name):
                with self.assertRaises(PatchError):
                    parse_unified_diff(source)

    def test_rejects_context_mismatch_without_leaking_source_content(self) -> None:
        """防止错误消息回显不匹配的原始文本。"""
        patch = parse_unified_diff(UPDATE_PATCH)[0]

        with self.assertRaises(PatchError) as raised:
            apply_file_patch("value = 1\nenabled = Unknown\n", patch)

        self.assertEqual("补丁上下文与原文不匹配", str(raised.exception))
        self.assertNotIn("enabled = Unknown", str(raised.exception))
