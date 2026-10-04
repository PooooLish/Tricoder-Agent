"""旧模块入口与新规范入口的兼容性回归。"""

from __future__ import annotations

import unittest


class ModuleCompatibilityTests(unittest.TestCase):
    def test_process_symbols_are_single_objects(self) -> None:
        """兼容模块不能复制类型、函数或进程清理状态。"""

        from tricoder import subprocess_control as old_control
        from tricoder import subprocess_env as old_env
        from tricoder.process import control as new_control
        from tricoder.process import env as new_env

        for name in (
            "BoundedProcessResult",
            "ProcessExecutionUncertain",
            "_WindowsJob",
            "run_bounded_process",
        ):
            with self.subTest(module="control", name=name):
                self.assertIs(getattr(old_control, name), getattr(new_control, name))

        for name in (
            "filtered_subprocess_env",
            "trusted_path_executable",
            "trusted_python_executable",
        ):
            with self.subTest(module="env", name=name):
                self.assertIs(getattr(old_env, name), getattr(new_env, name))

    def test_workspace_symbols_are_single_objects(self) -> None:
        """工作区锁、快照和门禁类型只能有一份权威定义。"""

        from tricoder import verification as old_verification
        from tricoder import workspace_gate as old_gate
        from tricoder import workspace_lock as old_lock
        from tricoder import workspace_snapshot as old_snapshot
        from tricoder.workspace import gate as new_gate
        from tricoder.workspace import lock as new_lock
        from tricoder.workspace import snapshot as new_snapshot
        from tricoder.workspace import verification as new_verification

        groups = (
            (
                old_lock,
                new_lock,
                (
                    "CONTROL_DIRECTORY",
                    "WorkspaceLock",
                    "WorkspaceLockError",
                    "WorkspaceLockBusyError",
                    "WorkspaceIdentityError",
                    "WorkspaceRecoveryRequiredError",
                ),
            ),
            (
                old_verification,
                new_verification,
                (
                    "WorkspaceSnapshot",
                    "VerificationEvidence",
                    "VerificationScope",
                    "stable_snapshots",
                    "proves_new_file_version",
                    "_bound_directory",
                    "_is_reparse",
                    "_metadata",
                    "_open_binary",
                ),
            ),
            (
                old_snapshot,
                new_snapshot,
                (
                    "WorkspaceBaseline",
                    "FileSnapshotEntry",
                    "SnapshotLimits",
                    "WorkspaceScanError",
                    "WorkspaceChangePreview",
                    "capture_workspace_baseline",
                    "compare_baselines",
                    "task_changes_match_baselines",
                ),
            ),
            (
                old_gate,
                new_gate,
                (
                    "WorkspaceGate",
                    "WorkspaceGateError",
                    "WorkspaceConfirmationRejected",
                    "WorkspaceConfirmationUnavailable",
                    "WorkspaceGateCancelled",
                    "WorkspaceGatePreview",
                ),
            ),
        )
        for old_module, new_module, names in groups:
            for name in names:
                with self.subTest(module=new_module.__name__, name=name):
                    self.assertIs(
                        getattr(old_module, name),
                        getattr(new_module, name),
                    )

    def test_session_symbols_are_single_objects(self) -> None:
        """Session 锁、存储和 Runtime 不能因兼容入口形成两份状态。"""

        from tricoder import session_lock as old_lock
        from tricoder import session_runtime as old_runtime
        from tricoder import sessions as old_store
        from tricoder.session import lock as new_lock
        from tricoder.session import runtime as new_runtime
        from tricoder.session import store as new_store

        groups = (
            (old_lock, new_lock, ("SessionLock", "SessionLockBusyError")),
            (
                old_store,
                new_store,
                (
                    "SessionError",
                    "SessionStore",
                    "default_sessions_db",
                    "safe_requirement_summary",
                    "safe_result_summary",
                    "validate_session_name",
                ),
            ),
            (
                old_runtime,
                new_runtime,
                (
                    "SessionRuntime",
                    "SessionRuntimeError",
                    "SessionInUseError",
                    "RuntimeOptions",
                    "ActiveSession",
                    "RuntimeStatus",
                    "MemorySavePreview",
                    "MemoryEditPreview",
                    "MemoryArchiveDeletePreview",
                    "ContextAgent",
                ),
            ),
        )
        for old_module, new_module, names in groups:
            for name in names:
                with self.subTest(module=new_module.__name__, name=name):
                    self.assertIs(
                        getattr(old_module, name),
                        getattr(new_module, name),
                    )

    def test_presentation_symbols_are_single_objects(self) -> None:
        """命令、审批和界面入口的兼容导入必须指向规范实现。"""

        from tricoder import approval_wait as old_approval
        from tricoder import commands as old_commands
        from tricoder import shell as old_shell
        from tricoder import tui as old_tui
        from tricoder import ui as old_console
        from tricoder.presentation import approval_wait as new_approval
        from tricoder.presentation import commands as new_commands
        from tricoder.presentation import console as new_console
        from tricoder.presentation import shell as new_shell
        from tricoder.presentation import tui as new_tui

        groups = (
            (
                old_commands,
                new_commands,
                (
                    "CommandError",
                    "CommandSpec",
                    "ParsedCommand",
                    "list_commands",
                    "command_spec",
                    "is_slash_command",
                    "parse_command",
                ),
            ),
            (old_approval, new_approval, ("ApprovalWait",)),
            (old_console, new_console, ("TerminalUI",)),
            (old_shell, new_shell, ("InteractiveShell", "RuntimeLike", "ShellUI")),
            (
                old_tui,
                new_tui,
                (
                    "ApprovalScreen",
                    "OptionChoice",
                    "OptionListScreen",
                    "TextInputScreen",
                    "TuiObserver",
                    "TricoderApp",
                ),
            ),
        )
        for old_module, new_module, names in groups:
            for name in names:
                with self.subTest(module=new_module.__name__, name=name):
                    self.assertIs(
                        getattr(old_module, name),
                        getattr(new_module, name),
                    )

        self.assertTrue(old_commands.is_slash_command("/status"))
        self.assertEqual("status", old_commands.parse_command("/status").name)


if __name__ == "__main__":
    unittest.main()
