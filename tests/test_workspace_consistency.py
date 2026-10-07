"""工作区变化对验证、Agent 视图与任务收尾基线的影响。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MethodType

from tricoder.changes import FileChange, FileIdentity, FileSnapshot, TaskChangeSet
from tricoder.audit import AuditLogger
from tricoder.core.validation import CommandCheckRecord, TaskValidationReport
from tricoder.models import (
    AppConfig,
    Message,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionMemory,
    SessionTurnResult,
)
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore
from tricoder.workspace.gate import WorkspaceGatePreview
from tricoder.workspace.snapshot import WorkspaceScanError, capture_workspace_baseline


class _InspectingAgent:
    def __init__(self, callback=None) -> None:  # type: ignore[no-untyped-def]
        self.calls: list[tuple[str, SessionContext]] = []
        self.callback = callback

    def run_with_context(self, task, context, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((task, context))
        if self.callback is not None:
            self.callback(task, context)
        return SessionTurnResult(RunResult(True, "synthetic", 1), context)


def _builder(agent: _InspectingAgent):
    def build(record, memory, _options):  # type: ignore[no-untyped-def]
        return ActiveSession(
            record,
            memory,
            SessionContext(
                messages=(Message("user", "保留的用户约束", kind="generic"),),
                persisted_summary=memory.summary,
                verification=memory.verification,
            ),
            AppConfig(
                workspace=record.workspace,
                provider=ProviderConfig("openai", "synthetic", "https://example.invalid", "model"),
            ),
            agent,
        )

    return build


class WorkspaceConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "code.py").write_text("value = 1\n", encoding="utf-8")
        self.store = SessionStore(self.root / "state" / "sessions.db")
        self.store.initialize(self.workspace)

    def runtime(self, agent: _InspectingAgent, **kwargs) -> SessionRuntime:  # type: ignore[no-untyped-def]
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(agent),
            **kwargs,
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_detected_change_invalidates_old_verification_even_when_rejected(self) -> None:
        """若拒绝后仍显示 passed，状态会声称旧代码版本已经验证。"""

        agent = _InspectingAgent()
        runtime = self.runtime(agent, workspace_confirmer=lambda _preview: False)
        runtime.run_task("baseline")
        active = runtime._require_active_session()
        runtime.current = replace(
            active,
            memory=replace(active.memory, verification="passed"),
            context=replace(active.context, verification="passed"),
        )
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        with self.assertRaises(SessionRuntimeError):
            runtime.run_task("reject")

        current = runtime._require_active_session()
        self.assertEqual("待验证", current.context.verification)
        self.assertEqual("待验证", current.memory.verification)
        self.assertIsNone(current.context.verification_evidence)
        self.assertEqual(1, len(agent.calls))

    def test_accepted_change_adds_transient_safe_notice_and_preserves_history(self) -> None:
        """若只改摘要或注入原始 diff，Agent 会缺提示或把不可信源码当系统指令。"""

        agent = _InspectingAgent()
        runtime = self.runtime(agent, workspace_confirmer=lambda _preview: True)
        runtime.run_task("baseline")
        (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        runtime.run_task("inspect-change")

        _, received = agent.calls[-1]
        self.assertIn("工作区已变化", received.workspace_change_notice)
        self.assertIn("code.py", received.workspace_change_notice)
        self.assertNotIn("value = 2", received.workspace_change_notice)
        self.assertIn("保留的用户约束", tuple(message.content for message in received.messages))
        self.assertEqual("", runtime._require_active_session().context.workspace_change_notice)

    def test_unattributed_end_change_is_not_absorbed_and_next_task_requires_confirmation(self) -> None:
        """若成功返回就更新基线，编辑器或未知命令的写入会被静默信任。"""

        def external_write(task: str, _context: SessionContext) -> None:
            if task == "write-outside-journal":
                (self.workspace / "external.py").write_text("unknown = True\n", encoding="utf-8")

        agent = _InspectingAgent(external_write)
        previews: list[WorkspaceGatePreview] = []
        runtime = self.runtime(agent, workspace_confirmer=lambda preview: previews.append(preview) or False)
        runtime.run_task("baseline")

        result = runtime.run_task("write-outside-journal")

        self.assertFalse(result.ok)
        self.assertIn("收尾发现未归属的工作区变化", result.summary)
        with self.assertRaisesRegex(SessionRuntimeError, "已拒绝工作区变化"):
            runtime.run_task("next")
        self.assertEqual(("external.py",), previews[-1].changed_paths)
        self.assertEqual(2, len(agent.calls))

    def test_unattributed_end_change_marks_command_checks_stale(self) -> None:
        """Agent 返回后的外部写入不能留下 observed 的旧快照记录。"""

        runtime = self.runtime(_InspectingAgent())
        runtime.run_task("baseline")
        report = TaskValidationReport(
            records=(
                CommandCheckRecord(
                    task_id="task",
                    check_id="check",
                    argv=("python", "-m", "unittest", "test_code.py"),
                    cwd=".",
                    kind="tests",
                    returncode=0,
                    output_summary="OK",
                    execution_complete=True,
                    workspace_stable=True,
                    targets=("test_code.py",),
                    snapshot_id="snapshot-before-external-write",
                ),
            ),
            status="observed",
        )

        def controlled(_runtime: SessionRuntime, _task: str) -> RunResult:
            (self.workspace / "external.py").write_text(
                "unknown = True\n",
                encoding="utf-8",
            )
            return RunResult(True, "synthetic", 1, task_validation=report)

        runtime._run_task_locked = MethodType(controlled, runtime)  # type: ignore[method-assign]

        result = runtime.run_task("external-after-check")

        self.assertFalse(result.ok)
        self.assertEqual("stale", result.task_validation.status)
        self.assertIn(
            "工作区收尾状态与命令检查快照不一致",
            result.task_validation.limitations,
        )

    def test_external_overwrite_of_journal_path_is_not_absorbed(self) -> None:
        """路径相同不等于版本归属；工具 after 之后的外部覆盖必须保持待确认。"""

        agent = _InspectingAgent()
        previews: list[WorkspaceGatePreview] = []
        runtime = self.runtime(
            agent,
            workspace_confirmer=lambda preview: previews.append(preview) or False,
        )
        runtime.run_task("baseline")
        target = self.workspace / "code.py"

        def controlled(_runtime: SessionRuntime, _task: str) -> RunResult:
            before_stat = target.stat()
            before = FileSnapshot(
                "code.py",
                target.read_text("utf-8"),
                before_stat.st_mode & 0o7777,
                FileIdentity(before_stat.st_dev, before_stat.st_ino),
            )
            target.write_text("value = 2\n", encoding="utf-8")
            after_stat = target.stat()
            after = FileSnapshot(
                "code.py",
                "value = 2\n",
                after_stat.st_mode & 0o7777,
                FileIdentity(after_stat.st_dev, after_stat.st_ino),
            )
            _runtime._task_sealed_change_set = TaskChangeSet(
                (FileChange("code.py", before, after),),
                (),
                "待验证",
                ("code.py",),
                "通过",
            )
            # 模拟编辑器在工具已记录 after 后覆盖同一路径。
            target.write_text("value = 3\n", encoding="utf-8")
            return RunResult(True, "synthetic", 1, modified_files=("code.py",), verification="通过")

        runtime._run_task_locked = MethodType(controlled, runtime)  # type: ignore[method-assign]
        result = runtime.run_task("owned-then-overwritten")

        self.assertFalse(result.ok)
        self.assertIn("未归属", result.summary)
        with self.assertRaisesRegex(SessionRuntimeError, "已拒绝工作区变化"):
            runtime.run_task("next")
        self.assertEqual(("code.py",), previews[-1].changed_paths)

    def test_end_scan_failure_keeps_old_baseline_and_marks_result_unverified(self) -> None:
        """若收尾扫描失败仍报告成功，当前代码版本没有可信状态。"""

        calls = 0

        def fail_on_end(root, limits, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 2:
                raise WorkspaceScanError("io_error")
            return capture_workspace_baseline(root, limits, **kwargs)

        agent = _InspectingAgent()
        runtime = self.runtime(agent, workspace_snapshotter=fail_on_end)

        result = runtime.run_task("scan-fails-on-end")

        self.assertFalse(result.ok)
        self.assertIn("工作区收尾扫描失败", result.summary)
        self.assertEqual("待验证", result.verification)

    def test_exact_session_audit_inside_workspace_is_framework_owned(self) -> None:
        """精确审计文件可随任务追加，但不能泛化排除同目录中的用户源码。"""

        audit_path = self.workspace / "audit" / "session.jsonl"
        audit = AuditLogger(audit_path)
        previews: list[WorkspaceGatePreview] = []

        def append_audit(task: str, _context: SessionContext) -> None:
            if task == "audit-task":
                audit.log({"event": "synthetic"})

        agent = _InspectingAgent(append_audit)

        def build_with_audit(record, memory, options):  # type: ignore[no-untyped-def]
            audit.prepare()
            return replace(_builder(agent)(record, memory, options), audit=audit)

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=build_with_audit,
            workspace_confirmer=lambda preview: previews.append(preview) or True,
        )
        self.addCleanup(runtime.close)

        result = runtime.run_task("audit-task")
        preview_count = len(previews)
        next_result = runtime.run_task("next")

        self.assertTrue(result.ok, result)
        self.assertTrue(next_result.ok, next_result)
        self.assertEqual(preview_count, len(previews))
        self.assertTrue(audit_path.is_file())

    def test_agent_exception_still_runs_end_scan_without_absorbing_changes(self) -> None:
        """主异常不能跳过收尾扫描，也不能把异常期间的写入并入可信基线。"""

        calls = 0

        def count_scans(root, limits, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return capture_workspace_baseline(root, limits, **kwargs)

        def write_then_fail(_task: str, _context: SessionContext) -> None:
            (self.workspace / "partial.py").write_text("partial = True\n", encoding="utf-8")
            raise LookupError("synthetic primary failure")

        agent = _InspectingAgent(write_then_fail)
        runtime = self.runtime(agent, workspace_snapshotter=count_scans)

        with self.assertRaisesRegex(LookupError, "primary failure"):
            runtime.run_task("fails-after-write")

        self.assertEqual(2, calls)
        self.assertIn(
            runtime._require_active_session().record.id,
            runtime._workspace_baseline_unresolved,
        )
        self.assertEqual("待验证", runtime._require_active_session().context.verification)

    def test_continuous_two_runtime_flow_reconfirms_then_reuses_accepted_baseline(self) -> None:
        """A/B 基线隔离、拒绝、确认期间再变化和后续 unchanged 必须连贯成立。"""

        agent_a = _InspectingAgent()
        previews: list[WorkspaceGatePreview] = []
        decisions = iter((False, True, True))

        def confirm(preview: WorkspaceGatePreview) -> bool:
            previews.append(preview)
            decision = next(decisions)
            if decision and len(previews) == 2:
                (self.workspace / "code.py").write_text("value = 3\n", encoding="utf-8")
            return decision

        runtime_a = self.runtime(agent_a, workspace_confirmer=confirm)
        self.assertTrue(runtime_a.run_task("a-baseline").ok)

        def b_write(_task: str, _context: SessionContext) -> None:
            (self.workspace / "code.py").write_text("value = 2\n", encoding="utf-8")

        agent_b = _InspectingAgent(b_write)
        store_b = SessionStore(self.root / "state-b" / "sessions.db")
        runtime_b = SessionRuntime(
            store_b,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_builder(agent_b),
            workspace_confirmer=lambda _preview: True,
        )
        self.addCleanup(runtime_b.close)
        self.assertFalse(runtime_b.run_task("b-external-change").ok)

        with self.assertRaisesRegex(SessionRuntimeError, "已拒绝工作区变化"):
            runtime_a.run_task("a-reject")
        self.assertEqual(["a-baseline"], [task for task, _ in agent_a.calls])

        # 第一次同意后又变化，因此同一请求必须展示第二个候选；随后接受的
        # 版本成为 A 的基线，下一任务不应重复提示。
        runtime_a.run_task("a-accept-after-rescan")
        runtime_a.run_task("a-unchanged-next")

        self.assertEqual(3, len(previews))
        self.assertNotEqual(previews[1].candidate_id, previews[2].candidate_id)
        self.assertEqual(
            ["a-baseline", "a-accept-after-rescan", "a-unchanged-next"],
            [task for task, _ in agent_a.calls],
        )
