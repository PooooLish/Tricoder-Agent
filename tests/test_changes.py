import sys
import unittest
from pathlib import Path


# 让未安装包的源码工作树可由 ``python -m unittest`` 直接执行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tricoder.changes import (
    ChangeBudgetError,
    ChangeJournal,
    ChangeJournalError,
    FileChange,
    FileIdentity,
    FileSnapshot,
)


def snapshot(path: str, content: str, inode: int) -> FileSnapshot:
    return FileSnapshot(path, content, 0o644, FileIdentity(1, inode))


class ChangeJournalTests(unittest.TestCase):
    def test_same_file_keeps_first_before_and_last_after(self) -> None:
        """防止连续修改同一文件时丢失最早快照或最终快照。"""
        journal = ChangeJournal()
        journal.begin_task(("old.py",), "passed")
        first = snapshot("app.py", "value = 1\n", 10)
        middle = snapshot("app.py", "value = 2\n", 11)
        final = snapshot("app.py", "value = 3\n", 12)

        journal.record_committed("app.py", first, middle)
        journal.record_committed("app.py", middle, final)
        result = journal.seal_task(("old.py", "app.py"), "not-run")

        self.assertEqual((FileChange("app.py", first, final),), result.changes)
        self.assertEqual(("old.py",), result.before_modified_files)
        self.assertEqual("passed", result.before_verification)

    def test_net_zero_task_keeps_previous_non_empty_change_set(self) -> None:
        """防止无净变化的后续任务覆盖可撤销的上一组变更。"""
        journal = ChangeJournal()
        before = snapshot("app.py", "a\n", 10)
        after = snapshot("app.py", "b\n", 11)
        journal.begin_task((), "not-run")
        journal.record_committed("app.py", before, after)
        previous = journal.seal_task(("app.py",), "not-run")
        journal.begin_task(("app.py",), "not-run")
        journal.record_committed("app.py", after, before)
        journal.record_committed("app.py", before, after)

        self.assertIsNone(journal.seal_task(("app.py",), "not-run"))
        self.assertEqual(previous, journal.latest())

    def test_create_then_compensate_back_to_missing_removes_net_change(self) -> None:
        """防止创建后删除仍被错误地保留为净变化。"""
        journal = ChangeJournal()
        created = snapshot("new.py", "created\n", 20)
        journal.begin_task((), "not-run")
        journal.record_committed("new.py", None, created)
        journal.record_committed("new.py", created, None)

        self.assertIsNone(journal.seal_task((), "not-run"))

    def test_record_committed_requires_active_task(self) -> None:
        """防止未开始任务时写入无法归属的变更。"""
        journal = ChangeJournal()

        with self.assertRaisesRegex(ChangeJournalError, "未开始"):
            journal.record_committed("app.py", None, snapshot("app.py", "x", 1))

    def test_begin_task_rejects_nested_active_task(self) -> None:
        """防止嵌套任务覆盖尚未封存的变更记录。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")

        with self.assertRaisesRegex(ChangeJournalError, "尚未封存"):
            journal.begin_task((), "not-run")

    def test_clear_latest_removes_sealed_change_set(self) -> None:
        """防止清除撤销目标后仍返回过期的变更集。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")
        journal.record_committed("app.py", None, snapshot("app.py", "x", 1))
        journal.seal_task(("app.py",), "passed")

        journal.clear_latest()

        self.assertIsNone(journal.latest())

    def test_rejects_snapshot_path_that_differs_from_record_path(self) -> None:
        """防止路径键与快照内容不一致而破坏变更归属。"""
        journal = ChangeJournal()
        journal.begin_task((), "not-run")

        with self.assertRaisesRegex(ChangeJournalError, "路径"):
            journal.record_committed("app.py", None, snapshot("other.py", "x", 1))

    def test_reserve_counts_projected_net_before_and_after_characters(self) -> None:
        """防止预算遗漏拟议净变化两侧的文件内容。"""
        journal = ChangeJournal(max_chars=5)
        journal.begin_task((), "not-run")
        proposed = (FileChange("a.py", None, snapshot("a.py", "123456", 1)),)

        with self.assertRaisesRegex(ChangeBudgetError, "2,000,000|预算"):
            journal.reserve(proposed)

        self.assertIsNone(journal.seal_task((), "not-run"))

    def test_reserve_allows_limit_boundary_and_does_not_double_count_replaced_after(self) -> None:
        """防止同一路径的拟议后态与既有后态被重复计入预算。"""
        journal = ChangeJournal(max_chars=8)
        before = snapshot("a.py", "aaa", 1)
        current_after = snapshot("a.py", "bbbb", 2)
        proposed_after = snapshot("a.py", "ccccc", 3)
        journal.begin_task((), "not-run")
        journal.record_committed("a.py", before, current_after)

        journal.reserve((FileChange("a.py", current_after, proposed_after),))
        result = journal.seal_task(("a.py",), "not-run")

        self.assertEqual((FileChange("a.py", before, current_after),), result.changes)


if __name__ == "__main__":
    unittest.main()
