"""跨重启工作区基线的稳定清单与纯比较回归。"""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from tricoder.workspace.baseline_record import (
    BaselineRecordError,
    baseline_record_from_json,
    baseline_record_to_json,
    compare_baseline_records,
    make_baseline_record,
)
from tricoder.workspace.snapshot import FileSnapshotEntry, WorkspaceBaseline


def _entry(
    path: str,
    *,
    kind: str = "text",
    size: int = 3,
    digest: str = "a" * 64,
    mode: int = 0o644,
    identity: tuple[int, ...] = (0o100000, 1, 10, 3, 1, 0),
    text: str | None = "x\n",
) -> FileSnapshotEntry:
    return FileSnapshotEntry(path, kind, size, digest, mode, identity, text)


def _baseline(
    *entries: FileSnapshotEntry,
    root_identity: tuple[int, ...] = (0o040000, 1, 2),
    complete: bool = True,
) -> WorkspaceBaseline:
    return WorkspaceBaseline(
        workspace_key="D:\\workspace",
        root_identity=root_identity,
        scope_version="workspace-baseline-v1",
        snapshot_id="runtime-only",
        complete=complete,
        entries=entries,
    )


class WorkspaceBaselineRecordTests(unittest.TestCase):
    def test_same_content_with_replaced_file_identity_is_equal(self) -> None:
        """把 entry identity 纳入稳定摘要会让编辑器原子保存反复误报变化。"""

        before = make_baseline_record(_baseline(_entry("app.py")))
        after = make_baseline_record(
            _baseline(
                _entry(
                    "app.py",
                    identity=(0o100000, 1, 99, 3, 1, 0),
                    text="同一正文不会进入记录",
                )
            )
        )

        comparison = compare_baseline_records(before, after)

        self.assertEqual(before.content_digest, after.content_digest)
        self.assertTrue(comparison.compatible)
        self.assertFalse(comparison.changed)
        self.assertEqual((), comparison.changes)

    def test_content_path_type_empty_directory_and_permission_changes_are_visible(self) -> None:
        """稳定投影不能因不保存正文而漏掉真实文件级变化。"""

        before = make_baseline_record(
            _baseline(
                _entry("changed.py", digest="1" * 64),
                _entry("deleted.py", digest="2" * 64),
                _entry("kind", kind="directory", size=0, digest="", mode=0o755, text=None),
                _entry("mode.py", digest="3" * 64, mode=0o644),
                _entry("empty", kind="directory", size=0, digest="", mode=0o755, text=None),
            )
        )
        after = make_baseline_record(
            _baseline(
                _entry("added.py", digest="4" * 64),
                _entry("changed.py", digest="5" * 64),
                _entry("kind", kind="text", digest="6" * 64),
                _entry("mode.py", digest="3" * 64, mode=0o600),
                _entry("new-empty", kind="directory", size=0, digest="", mode=0o755, text=None),
            )
        )

        comparison = compare_baseline_records(before, after)
        changes = {change.path: change.change_type for change in comparison.changes}

        self.assertTrue(comparison.compatible)
        self.assertTrue(comparison.changed)
        self.assertEqual(
            {
                "added.py": "added",
                "changed.py": "modified",
                "deleted.py": "deleted",
                "empty": "deleted",
                "kind": "type_changed",
                "mode.py": "permission_changed",
                "new-empty": "added",
            },
            changes,
        )

    def test_replaced_workspace_root_is_not_equal(self) -> None:
        """只比较文件清单会把同路径根目录替换误判为原工作区。"""

        before = make_baseline_record(_baseline(_entry("app.py")))
        after = make_baseline_record(
            _baseline(_entry("app.py"), root_identity=(0o040000, 1, 88))
        )

        comparison = compare_baseline_records(before, after)

        self.assertFalse(comparison.compatible)
        self.assertTrue(comparison.changed)
        self.assertEqual("root_identity_changed", comparison.reason)

    def test_payload_round_trip_excludes_source_text_and_runtime_identity(self) -> None:
        """序列化只能包含稳定清单，不能把 WorkspaceBaseline 正文直接入库。"""

        sentinel = "PRIVATE-SOURCE-SENTINEL"
        baseline = _baseline(
            _entry(
                "src/app.py",
                size=len(sentinel),
                digest="7" * 64,
                identity=(0o100000, 5, 987654321, len(sentinel), 1, 0),
                text=sentinel,
            )
        )

        record = make_baseline_record(baseline)
        payload = baseline_record_to_json(record)
        restored = baseline_record_from_json(payload)

        self.assertEqual(record, restored)
        self.assertNotIn(sentinel, payload)
        self.assertNotIn("987654321", payload)
        decoded = json.loads(payload)
        self.assertNotIn("text", decoded["entries"][0])
        self.assertEqual("file", decoded["entries"][0]["kind"])

    def test_incomplete_snapshot_is_rejected_before_projection(self) -> None:
        """部分扫描不能生成可被存储层误当完整历史的记录。"""

        with self.assertRaises(BaselineRecordError) as captured:
            make_baseline_record(replace(_baseline(_entry("app.py")), complete=False))

        self.assertEqual("incomplete", captured.exception.reason)

    def test_non_boolean_complete_is_rejected_even_with_matching_digest(self) -> None:
        """SQLite 中的非规范真值不能伪装成可信的完整扫描。"""

        record = make_baseline_record(_baseline(_entry("app.py")))
        payload = json.loads(baseline_record_to_json(record))
        payload["complete"] = 1
        digest_payload = {
            key: value for key, value in payload.items() if key != "content_digest"
        }
        payload["content_digest"] = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        with self.assertRaises(BaselineRecordError) as captured:
            baseline_record_from_json(json.dumps(payload))

        self.assertEqual("corrupted", captured.exception.reason)

    def test_external_json_field_types_raise_fixed_corruption_error(self) -> None:
        """外部 JSON 的异构类型必须归一为固定错误，不能泄漏 TypeError。"""

        record = make_baseline_record(_baseline(_entry("app.py")))
        original = json.loads(baseline_record_to_json(record))
        cases = {
            "kind-null": ("kind", None),
            "kind-boolean": ("kind", True),
            "kind-number": ("kind", 7),
            "kind-array": ("kind", []),
            "kind-object": ("kind", {}),
            "kind-invalid-string": ("kind", "unknown"),
            "path-object": ("path", {}),
            "size-boolean": ("size", True),
            "digest-array": ("digest", []),
            "mode-string": ("mode", "420"),
        }

        for name, (field, value) in cases.items():
            with self.subTest(name=name):
                payload = json.loads(json.dumps(original))
                payload["entries"][0][field] = value

                with self.assertRaises(BaselineRecordError) as captured:
                    baseline_record_from_json(json.dumps(payload))

                self.assertEqual("corrupted", captured.exception.reason)

    def test_external_json_top_level_types_raise_fixed_corruption_error(self) -> None:
        """顶层关键字段也必须先做结构和精确类型校验。"""

        record = make_baseline_record(_baseline(_entry("app.py")))
        original = json.loads(baseline_record_to_json(record))
        cases = {
            "scope-version-array": ("scope_version", []),
            "workspace-key-number": ("workspace_key", 3),
            "root-identity-object": ("root_identity", {}),
            "root-identity-boolean-member": ("root_identity", [1, True, 3]),
            "complete-string": ("complete", "true"),
            "entries-object": ("entries", {}),
            "content-digest-null": ("content_digest", None),
        }

        for name, (field, value) in cases.items():
            with self.subTest(name=name):
                payload = json.loads(json.dumps(original))
                payload[field] = value

                with self.assertRaises(BaselineRecordError) as captured:
                    baseline_record_from_json(json.dumps(payload))

                self.assertEqual("corrupted", captured.exception.reason)


if __name__ == "__main__":
    unittest.main()
