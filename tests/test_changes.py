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
    TaskChangeSet,
    render_change_set_diff,
)


def snapshot(path: str, content: str, inode: int) -> FileSnapshot:
    return FileSnapshot(path, content, 0o644, FileIdentity(1, inode))


class ChangeJournalTests(unittest.TestCase):
    @staticmethod
    def _change_set(*changes: FileChange) -> TaskChangeSet:
        return TaskChangeSet(tuple(changes), (), "not-run", (), "not-run")

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

    def test_diff_marks_missing_final_newlines_and_separates_multiple_files(self) -> None:
        """防止无末尾换行的 +/- 行粘连，或下一文件头接在源码行后。"""
        first_before = snapshot("a.py", "old", 1)
        first_after = snapshot("a.py", "new", 2)
        second_before = snapshot("b.py", "before\n", 3)
        second_after = snapshot("b.py", "after\n", 4)
        change_set = self._change_set(
            FileChange("b.py", second_before, second_after),
            FileChange("a.py", first_before, first_after),
        )

        forward = render_change_set_diff(change_set)
        reverse = render_change_set_diff(change_set, reverse=True)

        self.assertIn(
            "-old\n\\ No newline at end of file\n"
            "+new\n\\ No newline at end of file\n",
            forward,
        )
        self.assertIn("\n--- b.py\n+++ b.py\n", forward)
        self.assertIn(
            "-new\n\\ No newline at end of file\n"
            "+old\n\\ No newline at end of file\n",
            reverse,
        )
        self.assertIn("\n--- b.py\n+++ b.py\n", reverse)

    def test_diff_renders_trailing_newline_only_changes_in_both_directions(self) -> None:
        """防止仅新增或移除末尾换行时正反向 diff 为空。"""
        before = snapshot("newline.py", "same", 1)
        after = snapshot("newline.py", "same\n", 2)
        change_set = self._change_set(FileChange("newline.py", before, after))

        forward = render_change_set_diff(change_set)
        reverse = render_change_set_diff(change_set, reverse=True)

        self.assertIn(
            "-same\n\\ No newline at end of file\n+same\n",
            forward,
        )
        self.assertIn(
            "-same\n+same\n\\ No newline at end of file\n",
            reverse,
        )

    def test_diff_renders_mode_only_changes_in_both_directions(self) -> None:
        """防止仅权限变化得到空的正向或反向预览。"""
        before = FileSnapshot("mode.py", "same\n", 0o644, FileIdentity(1, 1))
        after = FileSnapshot("mode.py", "same\n", 0o755, FileIdentity(1, 2))
        change_set = self._change_set(FileChange("mode.py", before, after))

        forward = render_change_set_diff(change_set)
        reverse = render_change_set_diff(change_set, reverse=True)

        self.assertIn("--- mode.py\n+++ mode.py\n", forward)
        self.assertIn("old mode 0644\nnew mode 0755\n", forward)
        self.assertIn("old mode 0755\nnew mode 0644\n", reverse)


if __name__ == "__main__":
    unittest.main()
