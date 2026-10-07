"""单任务进展守卫的纯逻辑回归。"""

from __future__ import annotations

import hashlib
import unittest

from tricoder.core.validation import CommandCheckRecord
from tricoder.engine.progress import (
    ProgressAction,
    ProgressGuard,
    ProgressObservation,
    ProgressReason,
)
from tricoder.engine.tool_batch import ToolBatchExecutor
from tricoder.execution_state import ErrorCode, RecoveryAction
from tricoder.models import ToolResult, tool_failure


def observation(
    *,
    tool: str = "run_command",
    arguments: str = "args-a",
    result: str = "result-a",
    failure: str | None = "execution_failed:1",
    state: str | None = "state-a",
    complete: bool = True,
    read_only: bool = False,
    check: str | None = "check-a",
) -> ProgressObservation:
    """构造只含指纹、不含源码或工具输出的观察。"""

    return ProgressObservation(
        tool_name=tool,
        arguments_fingerprint=arguments,
        result_fingerprint=result,
        failure_classification=failure,
        workspace_fingerprint=state,
        workspace_complete=complete,
        read_only=read_only,
        check_fingerprint=check,
    )


class ProgressGuardTests(unittest.TestCase):
    def test_result_fingerprint_ignores_host_generated_duration_width(self) -> None:
        check = CommandCheckRecord(
            task_id="task-a",
            check_id="check-a",
            argv=("python", "--version"),
            cwd=".",
            kind="information",
            returncode=0,
            output_summary="version",
            execution_complete=True,
        )
        short = ToolBatchExecutor._result_fingerprint(
            ToolResult(True, "completed in 0.1s", command_check=check)
        )
        long = ToolBatchExecutor._result_fingerprint(
            ToolResult(True, "completed in 10.250 seconds", command_check=check)
        )

        self.assertEqual(short, long)

    def test_file_content_duration_text_is_not_normalized(self) -> None:
        short = ToolBatchExecutor._result_fingerprint(
            ToolResult(True, "completed in 0.1s")
        )
        long = ToolBatchExecutor._result_fingerprint(
            ToolResult(True, "completed in 10.250 seconds")
        )

        self.assertNotEqual(short, long)

    def test_trusted_progress_digest_ignores_spill_wrapper_and_reference(self) -> None:
        digest = hashlib.sha256(b"same complete output").hexdigest()
        first = ToolBatchExecutor._result_fingerprint(
            ToolResult(
                True,
                "wrapper with spill_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                spill_reference="spill_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                spill_sha256=hashlib.sha256(b"raw first").hexdigest(),
                progress_output_digest=digest,
            )
        )
        second = ToolBatchExecutor._result_fingerprint(
            ToolResult(
                True,
                "wrapper with spill_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                spill_reference="spill_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                spill_sha256=hashlib.sha256(b"raw second").hexdigest(),
                progress_output_digest=digest,
            )
        )

        self.assertEqual(first, second)

    def test_real_error_output_change_changes_result_fingerprint(self) -> None:
        first = ToolBatchExecutor._result_fingerprint(
            tool_failure(
                ErrorCode.EXECUTION_FAILED,
                "first diagnostic",
                recovery=RecoveryAction.REPLAN,
            )
        )
        second = ToolBatchExecutor._result_fingerprint(
            tool_failure(
                ErrorCode.EXECUTION_FAILED,
                "other diagnostic",
                recovery=RecoveryAction.REPLAN,
            )
        )

        self.assertNotEqual(first, second)

    def test_same_failure_warns_second_and_stops_third(self) -> None:
        guard = ProgressGuard()

        first = guard.observe(observation())
        second = guard.observe(observation())
        third = guard.observe(observation())

        self.assertIs(ProgressAction.CONTINUE, first.action)
        self.assertEqual(
            (ProgressAction.WARN, ProgressReason.REPEATED_FAILURE, 2, 3),
            (second.action, second.reason, second.count, second.limit),
        )
        self.assertEqual(
            (ProgressAction.STOP, ProgressReason.REPEATED_FAILURE, 3, 3),
            (third.action, third.reason, third.count, third.limit),
        )

    def test_ordinary_read_and_unrelated_success_do_not_clear_failure(self) -> None:
        guard = ProgressGuard()
        failed = observation()
        guard.observe(failed)
        guard.observe(
            observation(
                tool="read_file",
                arguments="read-b",
                result="contents-b",
                failure=None,
                read_only=True,
                check=None,
            )
        )
        guard.observe(
            observation(
                tool="list_files",
                arguments="list-c",
                result="listing-c",
                failure=None,
                read_only=True,
                check=None,
            )
        )

        self.assertIs(ProgressAction.WARN, guard.observe(failed).action)
        self.assertIs(ProgressAction.STOP, guard.observe(failed).action)

    def test_same_read_warns_second_and_stops_fourth_across_interleaving(self) -> None:
        guard = ProgressGuard()
        read_a = observation(
            tool="read_file",
            failure=None,
            read_only=True,
            check=None,
        )
        read_b = observation(
            tool="list_files",
            arguments="args-b",
            result="result-b",
            failure=None,
            read_only=True,
            check=None,
        )

        self.assertIs(ProgressAction.CONTINUE, guard.observe(read_a).action)
        guard.observe(read_b)
        second = guard.observe(read_a)
        guard.observe(read_b)
        third = guard.observe(read_a)
        fourth = guard.observe(read_a)

        self.assertEqual(
            (ProgressAction.WARN, ProgressReason.REPEATED_OBSERVATION, 2, 4),
            (second.action, second.reason, second.count, second.limit),
        )
        self.assertIs(ProgressAction.CONTINUE, third.action)
        self.assertEqual(
            (ProgressAction.STOP, ProgressReason.REPEATED_OBSERVATION, 4, 4),
            (fourth.action, fourth.reason, fourth.count, fourth.limit),
        )

    def test_user_answer_restarts_reads_but_not_failure_evidence(self) -> None:
        guard = ProgressGuard()
        failed = observation()
        read = observation(
            tool="read_file",
            failure=None,
            read_only=True,
            check=None,
        )
        guard.observe(failed)
        guard.observe(read)
        guard.observe(read)

        guard.note_user_answer()

        self.assertIs(ProgressAction.CONTINUE, guard.observe(read).action)
        self.assertIs(ProgressAction.WARN, guard.observe(failed).action)

    def test_workspace_change_restarts_reads_but_not_failure_evidence(self) -> None:
        guard = ProgressGuard()
        failed = observation()
        read = observation(
            tool="read_file",
            failure=None,
            read_only=True,
            check=None,
        )
        guard.observe(failed)
        guard.observe(read)
        guard.observe(read)
        guard.observe(read)

        guard.note_workspace_change()

        self.assertIs(ProgressAction.CONTINUE, guard.observe(read).action)
        self.assertIs(ProgressAction.WARN, guard.observe(failed).action)

    def test_workspace_change_does_not_clear_oscillation_evidence(self) -> None:
        guard = ProgressGuard()
        decisions = []
        for index, state in enumerate(("A", "B", "A", "B", "A"), start=1):
            decisions.append(
                guard.observe(observation(state=state, result=f"failure-{index}"))
            )
            guard.note_workspace_change()

        self.assertIs(ProgressAction.STOP, decisions[-1].action)
        self.assertIs(ProgressReason.REPAIR_OSCILLATION, decisions[-1].reason)

    def test_complete_a_b_a_b_a_failed_check_stops_as_oscillation(self) -> None:
        guard = ProgressGuard()
        decisions = [
            guard.observe(observation(state=state, result=f"failure-{index}"))
            for index, state in enumerate(("A", "B", "A", "B", "A"), start=1)
        ]

        self.assertEqual(
            (
                ProgressAction.STOP,
                ProgressReason.REPAIR_OSCILLATION,
                5,
                5,
            ),
            (
                decisions[-1].action,
                decisions[-1].reason,
                decisions[-1].count,
                decisions[-1].limit,
            ),
        )

    def test_incomplete_workspace_never_proves_same_state_or_oscillation(self) -> None:
        guard = ProgressGuard()
        decisions = [
            guard.observe(observation(state=None, complete=False))
            for _ in range(5)
        ]

        self.assertTrue(
            all(decision.action is ProgressAction.CONTINUE for decision in decisions)
        )

    def test_guard_keeps_only_recent_32_observations(self) -> None:
        guard = ProgressGuard()
        first = observation(arguments="evicted")
        guard.observe(first)
        for index in range(32):
            guard.observe(
                observation(
                    tool="read_file",
                    arguments=f"read-{index}",
                    result=f"result-{index}",
                    failure=None,
                    read_only=True,
                    check=None,
                )
            )

        self.assertEqual(32, guard.observation_count)
        self.assertIs(ProgressAction.CONTINUE, guard.observe(first).action)

    def test_new_guard_does_not_inherit_previous_task_counts(self) -> None:
        old = ProgressGuard()
        old.observe(observation())
        self.assertIs(ProgressAction.WARN, old.observe(observation()).action)

        fresh = ProgressGuard()

        self.assertIs(ProgressAction.CONTINUE, fresh.observe(observation()).action)


if __name__ == "__main__":
    unittest.main()
