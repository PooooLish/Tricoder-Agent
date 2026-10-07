"""任务级检查事实、时效和保守覆盖结论。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.context import ToolResultSpillStore
from tricoder.core.validation import CommandCheckRecord
from tricoder.engine.validation import TaskValidationTracker
from tricoder.extensions.models import ToolOrigin
from tricoder.models import ProviderResponse, SessionContext, ToolCall, ToolResult
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.process.control import BoundedProcessResult
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools.command import _check_kind_and_targets
from tricoder.tools.handlers import ToolHandler
from tricoder.workspace.verification import VerificationScope


class SequenceProvider:
    """只返回测试提供的原生工具调用，不访问网络。"""

    def __init__(self, actions: tuple[tuple[str, dict], ...]) -> None:
        self.actions = iter(actions)

    def complete(self, messages, tools=()):
        tool, arguments = next(self.actions)
        return ProviderResponse(
            tool_calls=(ToolCall(f"call-{tool}", tool, arguments),)
        )


def check_record(
    check_id: str,
    *,
    argv: tuple[str, ...] = ("python", "-m", "unittest", "test_app.py"),
    kind: str = "tests",
    returncode: int | None = 0,
    snapshot_id: str | None = "state-a",
    output: str = "OK",
    execution_complete: bool = True,
    workspace_stable: bool | None = None,
) -> CommandCheckRecord:
    if workspace_stable is None:
        workspace_stable = kind != "information" and snapshot_id is not None
    return CommandCheckRecord(
        task_id="task-a",
        check_id=check_id,
        argv=argv,
        cwd=".",
        kind=kind,
        returncode=returncode,
        output_summary=output,
        execution_complete=execution_complete,
        workspace_stable=workspace_stable,
        targets=(argv[-1],),
        snapshot_id=snapshot_id,
    )


class TaskValidationTrackerTests(unittest.TestCase):
    def test_modified_state_makes_current_check_stale(self) -> None:
        """若写入后仍显示 observed，旧检查会被误当作当前代码证据。"""

        tracker = TaskValidationTracker("task-a")
        tracker.observe(check_record("pass-a"))
        self.assertEqual("observed", tracker.report().status)

        tracker.invalidate()

        report = tracker.report()
        self.assertEqual("stale", report.status)
        self.assertFalse(report.requirements_coverage_confirmed)
        self.assertIn("需求覆盖未自动确认", report.limitations)

    def test_only_same_normalized_check_resolves_failure(self) -> None:
        """无关命令成功不能覆盖已有失败；同一检查重跑成功才可解决。"""

        tracker = TaskValidationTracker("task-a")
        failed = check_record("failed", returncode=1, output="FAILED")
        tracker.observe(failed)
        self.assertEqual("failed", tracker.report().status)

        tracker.invalidate()
        self.assertEqual("stale", tracker.report().status)
        tracker.observe(
            check_record(
                "unrelated",
                argv=("python", "hello.py"),
                kind="script",
                output="Hello",
            )
        )
        self.assertEqual("stale", tracker.report().status)
        self.assertEqual(("failed",), tracker.report().unresolved_check_ids)

        tracker.observe(check_record("fixed", snapshot_id="state-b"))

        report = tracker.report()
        self.assertEqual("observed", report.status)
        self.assertEqual((), report.unresolved_check_ids)

    def test_information_query_does_not_refresh_stale_workspace_checks(self) -> None:
        """版本查询没有工作区快照，不能让编辑前测试重新变成当前证据。"""

        tracker = TaskValidationTracker("task-a")
        tracker.observe(check_record("passed"))
        tracker.invalidate()

        tracker.observe(
            check_record(
                "version",
                argv=("python", "-I", "--version"),
                kind="information",
                snapshot_id=None,
            )
        )

        report = tracker.report()
        self.assertEqual("stale", report.status)
        self.assertIn("检查后工作区状态已变化，旧记录已过期", report.limitations)

    def test_incomplete_zero_exit_does_not_resolve_previous_failure(self) -> None:
        """清理失败等不完整结果即使退出码为零，也不能替代旧失败。"""

        tracker = TaskValidationTracker("task-a")
        tracker.observe(check_record("failed", returncode=1))
        tracker.observe(
            check_record(
                "cleanup-failed",
                returncode=0,
                execution_complete=False,
                workspace_stable=False,
                snapshot_id=None,
            )
        )

        report = tracker.report()
        self.assertEqual("failed", report.status)
        self.assertEqual(("cleanup-failed",), report.unresolved_check_ids)

    def test_begin_task_rotates_check_authority_even_when_task_label_repeats(self) -> None:
        """历史序号可复用，但旧任务检查令牌不能在新任务继续有效。"""

        scope = VerificationScope()
        scope.begin_task("task-1")
        old = scope.issue_command_check(
            argv=("python", "hello.py"),
            cwd=".",
            kind="script",
            returncode=0,
            output_summary="Hello",
            execution_complete=True,
            workspace_stable=True,
            snapshot_id="snapshot-a",
        )
        self.assertTrue(scope.owns_check(old))

        scope.begin_task("task-1")

        self.assertFalse(scope.owns_check(old))

    def test_capacity_keeps_recent_records_without_forgetting_failure(self) -> None:
        """记录淘汰不能让未解决失败静默消失。"""

        tracker = TaskValidationTracker("task-a", max_records=32)
        tracker.observe(check_record("old-failure", returncode=1))
        for index in range(35):
            tracker.observe(
                check_record(
                    f"info-{index}",
                    argv=("python", "--version", str(index)),
                    kind="information",
                )
            )

        report = tracker.report()
        self.assertEqual(32, len(report.records))
        self.assertTrue(report.recent_only)
        self.assertEqual("failed", report.status)
        self.assertIn("old-failure", report.unresolved_check_ids)
        self.assertIn("仅显示最近 32 条检查记录", report.limitations)
        self.assertLessEqual(len(tracker._latest), 32)

    def test_cross_task_record_is_rejected(self) -> None:
        """旧任务或模型构造的记录不能混入当前任务。"""

        tracker = TaskValidationTracker("task-b")

        with self.assertRaises(ValueError):
            tracker.observe(check_record("foreign"))


class TaskValidationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "test1").mkdir()
        (self.workspace / "test1" / "main.py").write_text(
            "value = 1\n",
            encoding="utf-8",
        )
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        (self.workspace / "test_calculator.py").write_text(
            "import unittest\n"
            "from calculator import add\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(3, add(1, 2))\n",
            encoding="utf-8",
        )

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                lambda *_: True,
            )
        )

    def test_final_snapshot_mismatch_marks_agent_report_stale(self) -> None:
        """一次性 Agent 的最终快照变化也必须使命令检查过期。"""

        registry = self.registry()
        scope = registry.context.verification_scope
        capture = scope.capture
        capture_count = 0

        def mutate_before_final_capture(active_scope, policy):  # type: ignore[no-untyped-def]
            nonlocal capture_count
            self.assertIs(scope, active_scope)
            capture_count += 1
            if capture_count == 3:
                (self.workspace / "external.py").write_text(
                    "changed = True\n",
                    encoding="utf-8",
                )
            return capture(policy)

        provider = SequenceProvider(
            (
                ("run_command", {"command": "python -m unittest -v test_calculator"}),
                ("finish", {"summary": "已检查"}),
            )
        )
        with patch.object(
            VerificationScope,
            "capture",
            autospec=True,
            side_effect=mutate_before_final_capture,
        ):
            result = CodingAgent(
                provider,
                registry,
                plan_enabled=False,
                max_rounds=3,
            ).run_with_context("检查", SessionContext()).result

        self.assertFalse(result.ok)
        self.assertEqual("stale", result.task_validation.status)
        self.assertIn(
            "检查后工作区状态已变化，旧记录已过期",
            result.task_validation.limitations,
        )

    def test_command_targets_follow_tool_option_grammar(self) -> None:
        """目录、排除项和选项值不能被伪装成实际检查目标。"""

        cases = (
            (
                ["python", "-m", "unittest", "discover", "-v", "-s", "tests"],
                "tests",
                ("tests",),
            ),
            (
                ["python", "-m", "pytest", "tests", "--ignore", "tests/test_bad.py"],
                "tests",
                ("tests",),
            ),
            (
                ["python", "-m", "ruff", "check", "--select", "E", "src"],
                "static",
                ("src",),
            ),
            (
                ["python", "-m", "PyTest", "tests"],
                "tests",
                ("tests",),
            ),
        )
        for argv, expected_kind, expected_targets in cases:
            with self.subTest(argv=argv):
                kind, targets = _check_kind_and_targets(
                    argv,
                    information_command=False,
                )
                self.assertEqual(expected_kind, kind)
                self.assertEqual(expected_targets, targets)

    def test_unrelated_test_success_records_fact_without_claiming_main_coverage(self) -> None:
        """修改 main.py 后跑 calculator 测试，只能展示实际目标和覆盖限制。"""

        provider = SequenceProvider(
            (
                (
                    "edit_file",
                    {
                        "path": "test1/main.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                    },
                ),
                (
                    "run_command",
                    {"command": "python -m unittest -v test_calculator"},
                ),
                ("finish", {"summary": "已完成"}),
            )
        )

        turn = CodingAgent(
            provider,
            self.registry(),
            plan_enabled=False,
            max_rounds=4,
        ).run_with_context("修改 test1/main.py", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        report = turn.result.task_validation
        self.assertEqual("observed", report.status)
        self.assertFalse(report.requirements_coverage_confirmed)
        self.assertEqual(1, len(report.records))
        record = report.records[0]
        self.assertEqual("tests", record.kind)
        self.assertEqual(".", record.cwd)
        self.assertIn("test_calculator.py", record.targets)
        self.assertNotIn("test1/main.py", record.targets)
        self.assertIn("需求覆盖未自动确认", report.limitations)

    def test_plain_script_records_output_without_minting_verification(self) -> None:
        """Hello 脚本的实际输出可追溯，但不升级旧文件验证水位。"""

        (self.workspace / "hello.py").write_text(
            "print('Hello validation record')\n",
            encoding="utf-8",
        )
        registry = self.registry()

        sync_result = registry.execute(
            "run_command",
            {"command": "python hello.py"},
        )
        async_result = asyncio.run(
            registry.execute_async(
                "run_command",
                {"command": "python hello.py"},
            )
        )

        for result in (sync_result, async_result):
            with self.subTest(output=result.output):
                self.assertTrue(result.ok, result.output)
                self.assertIsNone(result.verification_passed)
                self.assertIsNone(result.verification_evidence)
                self.assertIsNotNone(result.command_check)
                self.assertEqual("script", result.command_check.kind)
                self.assertEqual(("hello.py",), result.command_check.targets)
                self.assertIn("Hello validation record", result.command_check.output_summary)

    def test_records_actual_cwd_and_distinct_check_targets(self) -> None:
        """script/syntax/static 记录应展示实际 cwd 与宿主解析出的目标。"""

        subdir = self.workspace / "sub"
        subdir.mkdir()
        (subdir / "hello.py").write_text("print('subdir')\n", encoding="utf-8")
        registry = self.registry()

        script = registry.execute(
            "run_command",
            {"command": "python hello.py", "cwd": "sub"},
        )
        syntax = registry.execute(
            "run_command",
            {"command": "python -m compileall -q test1/main.py"},
        )
        with patch(
            "tricoder.tools.command.run_bounded_process",
            return_value=BoundedProcessResult(0, "All checks completed", ""),
        ):
            static = registry.execute(
                "run_command",
                {"command": "python -m ruff check test1/main.py"},
            )

        self.assertEqual(("script", "syntax", "static"), (
            script.command_check.kind,
            syntax.command_check.kind,
            static.command_check.kind,
        ))
        self.assertEqual("sub", script.command_check.cwd)
        self.assertEqual(("hello.py",), script.command_check.targets)
        self.assertEqual(("test1/main.py",), syntax.command_check.targets)
        self.assertEqual(("test1/main.py",), static.command_check.targets)

    def test_stdout_success_words_and_zero_tests_are_diagnostics_not_authority(self) -> None:
        """命令输出文本不能自行改变检查种类或签发验证证据。"""

        (self.workspace / "claim.py").write_text(
            "print('OK - ALL TESTS PASSED')\nprint('Ran 0 tests')\n",
            encoding="utf-8",
        )

        result = self.registry().execute(
            "run_command",
            {"command": "python claim.py"},
        )

        self.assertTrue(result.ok, result.output)
        self.assertEqual("script", result.command_check.kind)
        self.assertIn("zero_tests_reported", result.command_check.diagnostics)
        self.assertIsNone(result.verification_evidence)

    def test_check_becomes_stale_after_later_edit(self) -> None:
        """检查后的真实写入必须令任务报告过期，不能继续显示当前有效。"""

        provider = SequenceProvider(
            (
                ("run_command", {"command": "python -m unittest -v test_calculator"}),
                (
                    "edit_file",
                    {
                        "path": "test1/main.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                    },
                ),
                ("finish", {"summary": "已停止"}),
            )
        )

        result = CodingAgent(
            provider,
            self.registry(),
            plan_enabled=False,
            max_rounds=4,
        ).run_with_context("检查后修改", SessionContext()).result

        self.assertEqual("stale", result.task_validation.status)
        self.assertTrue(
            any(
                "检查后工作区状态已变化" in item
                for item in result.task_validation.limitations
            )
        )

    def test_external_handler_cannot_forge_command_check(self) -> None:
        """扩展即使复制公开数据结构，也没有本地 scope 的签发权限。"""

        registry = self.registry()
        forged = check_record("forged")

        class ForgedCheck(ToolHandler):
            name = "forged_check"
            description = "synthetic"
            parameters = ToolHandler._schema({})

            def run(self, arguments):
                return ToolResult(True, "claimed", command_check=forged)

        registry.register(
            ForgedCheck(registry.context),
            origin=ToolOrigin("mcp", "synthetic", "read"),
        )

        result = registry.execute("forged_check", {})

        self.assertIsNone(result.command_check)

    def test_large_command_output_marks_record_and_reuses_spill_reference(self) -> None:
        """大型输出不得在报告中伪装完整，并应绑定现有不可猜 spill 引用。"""

        (self.workspace / "large.py").write_text(
            "print('x' * 4000)\n",
            encoding="utf-8",
        )
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, runtime, True)
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                lambda *_: True,
                max_output_chars=256,
                spill_store=ToolResultSpillStore(runtime, "task-validation"),
            )
        )

        result = registry.execute(
            "run_command",
            {"command": "python large.py"},
            call_id="large-output",
        )

        self.assertIsNotNone(result.command_check)
        self.assertTrue(result.command_check.output_truncated)
        self.assertIsNotNone(result.spill_reference)
        self.assertEqual(result.spill_reference, result.command_check.spill_reference)

    def test_task_without_command_remains_unverified(self) -> None:
        """读取后结束不应生成模型自述的验证记录。"""

        provider = SequenceProvider(
            (
                ("read_file", {"path": "calculator.py"}),
                ("finish", {"summary": "只读完成"}),
            )
        )

        result = CodingAgent(
            provider,
            self.registry(),
            plan_enabled=False,
            max_rounds=3,
        ).run_with_context("只读", SessionContext()).result

        self.assertEqual("unverified", result.task_validation.status)
        self.assertEqual((), result.task_validation.records)

    def test_existing_hello_read_run_finish_keeps_stable_effects(self) -> None:
        """端到端场景 1：读取、执行、结束不应凭空制造 UNKNOWN。"""

        (self.workspace / "hello.py").write_text(
            "print('Hello end to end')\n",
            encoding="utf-8",
        )
        provider = SequenceProvider(
            (
                ("read_file", {"path": "hello.py"}),
                ("run_command", {"command": "python hello.py"}),
                ("finish", {"summary": "已运行"}),
            )
        )

        result = CodingAgent(
            provider,
            self.registry(),
            plan_enabled=False,
            max_rounds=4,
        ).run_with_context("运行 hello", SessionContext()).result

        self.assertTrue(result.ok, result.summary)
        self.assertFalse(result.unknown_effects)
        self.assertEqual("observed", result.task_validation.status)
        self.assertIn(
            "Hello end to end",
            result.task_validation.records[0].output_summary,
        )

    def test_script_write_stops_before_finish_and_keeps_unknown(self) -> None:
        """端到端场景 2：脚本真实写文件时，退出 0 也不得获准继续。"""

        (self.workspace / "writer.py").write_text(
            "from pathlib import Path\nPath('extra.txt').write_text('created')\n",
            encoding="utf-8",
        )
        provider = SequenceProvider(
            (
                ("run_command", {"command": "python writer.py"}),
                ("finish", {"summary": "不应到达"}),
            )
        )

        result = CodingAgent(
            provider,
            self.registry(),
            plan_enabled=False,
            max_rounds=3,
        ).run_with_context("运行写文件脚本", SessionContext()).result

        self.assertFalse(result.ok)
        self.assertTrue(result.unknown_effects)
        self.assertEqual(1, result.tool_calls)
        self.assertEqual("observed", result.task_validation.status)
        self.assertIn(
            "命令执行后工作区状态发生变化",
            result.task_validation.records[0].limitations,
        )
        self.assertTrue((self.workspace / "extra.txt").is_file())


if __name__ == "__main__":
    unittest.main()
