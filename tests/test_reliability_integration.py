"""T1–T5 的端到端可靠性验收；所有场景只操作合成临时工作区。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.approval_wait import ApprovalWait
from tricoder.audit import AuditLogger
from tricoder.changes import ChangeJournal
from tricoder.execution_state import EffectState, FileEffects
from tricoder.extensions import ToolOrigin
from tricoder.models import (
    AppConfig,
    ProviderConfig,
    ProviderResponse,
    SessionContext,
    ToolCall,
    ToolResult,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.protocols import LegacyJsonProtocol
from tricoder.session_runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.sessions import SessionStore
from tricoder.shell import InteractiveShell
from tricoder.tools import ToolContext
from tricoder.tools.handlers import ToolHandler
from tricoder.tui import TricoderApp
from tests.test_agent import CallIdRecordingRegistry, ScriptedProvider, StructuredScriptedProvider
from tests.test_shell import FakeUI
from tests.test_tools import (
    patch_binding_publish_failure,
    patch_external_replacement_before_compensation,
)


def _batch(*calls: ToolCall) -> ProviderResponse:
    return ProviderResponse(tool_calls=tuple(calls), finish_reason="tool_calls")


def _runtime_with_provider(
    root: Path,
    state_dir: Path,
    provider: object,
    *,
    registry_type: type[CallIdRecordingRegistry] = CallIdRecordingRegistry,
    tool_protocol: str = "native",
    max_rounds: int = 12,
    approver=None,  # type: ignore[no-untyped-def]
) -> tuple[SessionRuntime, CallIdRecordingRegistry]:
    """用独立 SQLite、审计和真实 Runtime/Agent/Registry 组装一个场景。"""
    store = SessionStore(state_dir / "sessions.db")
    registries: list[CallIdRecordingRegistry] = []

    def factory(record, memory, _options):  # type: ignore[no-untyped-def]
        journal = ChangeJournal()
        registry = registry_type(
            ToolContext(
                WorkspacePolicy(root),
                CommandPolicy(root),
                approver or (lambda *_: True),
                change_journal=journal,
            )
        )
        registries.append(registry)
        config = AppConfig(
            root,
            ProviderConfig("openai", "synthetic", "https://example.test", "test"),
        )
        audit = AuditLogger(state_dir / "audit" / f"{record.id}.jsonl")
        agent = CodingAgent(
            provider,  # type: ignore[arg-type]
            registry,
            plan_enabled=False,
            max_rounds=max_rounds,
            tool_protocol=tool_protocol,
            audit=audit,
        )
        context = SessionContext(
            persisted_summary=memory.summary,
            modified_files=memory.modified_files,
            verification=memory.verification,
            unknown_effects=memory.unknown_effects,
        )
        return ActiveSession(record, memory, context, config, agent, registry, journal, audit)

    runtime = SessionRuntime(
        store,
        root,
        options=RuntimeOptions(environ={}),
        active_session_factory=factory,
    )
    return runtime, registries[-1]


class ReliabilityIntegrationTests(unittest.TestCase):
    """每个方法对应计划第 9 节的一条用户场景。"""

    def test_i01_partial_patch_skips_batch_then_repairs_and_verifies(self) -> None:
        """补丁留下首文件后，旧批次不执行；新轮读取、修复并重新验证。"""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "workspace"
            root.mkdir()
            first = root / "app.py"
            second = root / "other.py"
            first.write_text("value = 1\n", encoding="utf-8")
            second.write_text("value = 1\n", encoding="utf-8")
            class FirstPatchFailureRegistry(CallIdRecordingRegistry):
                """只在首个补丁发布中注入故障，不污染后续真实修复。"""

                async def execute_async(self, name, arguments, **kwargs):  # type: ignore[no-untyped-def]
                    if name == "apply_patch":
                        with patch_binding_publish_failure(rollback_fails=True):
                            return await super().execute_async(name, arguments, **kwargs)
                    return await super().execute_async(name, arguments, **kwargs)

            patch_text = (
                "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
                "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
            )
            first_batch = (
                ToolCall("patch", "apply_patch", {"patch": patch_text}),
                ToolCall("old-check", "run_command", {"command": "python -m compileall -q app.py other.py"}),
                ToolCall("old-finish", "finish", {"summary": "不能提前完成"}),
            )
            provider = StructuredScriptedProvider(
                [
                    _batch(*first_batch),
                    _batch(ToolCall("inspect", "read_file", {"path": "other.py"})),
                    _batch(
                        ToolCall(
                            "repair",
                            "edit_file",
                            {"path": "other.py", "old_text": "value = 1", "new_text": "value = 2"},
                        ),
                        ToolCall(
                            "new-check",
                            "run_command",
                            {"command": "python -m compileall -q app.py other.py"},
                        ),
                        ToolCall("new-finish", "finish", {"summary": "修复并检查完成"}),
                    ),
                ]
            )
            state_dir = base / "state"
            runtime, registry = _runtime_with_provider(
                root,
                state_dir,
                provider,
                registry_type=FirstPatchFailureRegistry,
            )
            result = runtime.run_task("应用补丁，失败后检查实际文件并修复")

            self.assertTrue(
                result.ok,
                (result, runtime.current.context, registry.call_ids, len(provider.histories)),
            )
            self.assertEqual("value = 2\n", first.read_text(encoding="utf-8"))
            self.assertEqual("value = 2\n", second.read_text(encoding="utf-8"))
            self.assertEqual(
                ["patch", "inspect", "repair", "new-check", "new-finish"],
                registry.call_ids,
                "失败批次中的旧检查和 finish 不得启动",
            )
            self.assertEqual(3, len(provider.histories))
            self.assertIsNotNone(runtime.current.context.verification_evidence)
            self.assertTrue(registry.context.verification_scope.owns(runtime.current.context.verification_evidence))
            persisted = runtime.store.load_memory(runtime.current.record.id)
            self.assertEqual(("app.py", "other.py"), persisted.modified_files)
            self.assertEqual("passed", persisted.verification)

            # Provider 发布的每个 call id 都必须得到恰好一个本地结果，包括被跳过项。
            tool_messages = [message for message in runtime.current.context.messages if message.role == "tool"]
            result_ids = [message.tool_call_id for message in tool_messages]
            expected = {
                "patch",
                "old-check",
                "old-finish",
                "inspect",
                "repair",
                "new-check",
                "new-finish",
            }
            self.assertEqual(expected, set(result_ids))
            self.assertEqual(len(expected), len(result_ids))
            payloads = {
                message.tool_call_id: json.loads(message.content or "{}")["tool_result"]
                for message in tool_messages
            }
            self.assertEqual("skipped", payloads["old-check"]["error"]["code"])
            self.assertEqual("skipped", payloads["old-finish"]["error"]["code"])

    def test_i02_identity_conflict_is_unknown_persisted_and_visible(self) -> None:
        """补偿遇到外部身份替换后立即停止，并从本地命令展示已知冲突。"""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "workspace"
            root.mkdir()
            first = root / "app.py"
            second = root / "other.py"
            first.write_text("value = 1\n", encoding="utf-8")
            second.write_text("value = 1\n", encoding="utf-8")
            fault_state: dict[str, object] = {}

            class IdentityConflictRegistry(CallIdRecordingRegistry):
                """仅给首个补丁注入真实 identity 冲突，保留 Registry 其他边界。"""

                async def execute_async(self, name, arguments, **kwargs):  # type: ignore[no-untyped-def]
                    if name == "apply_patch":
                        with patch_external_replacement_before_compensation(fault_state):
                            return await super().execute_async(name, arguments, **kwargs)
                    return await super().execute_async(name, arguments, **kwargs)

            patch_text = (
                "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
                "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
            )
            calls = (
                ToolCall("conflict", "apply_patch", {"patch": patch_text}),
                ToolCall("must-not-run", "run_command", {"command": "python -m compileall -q app.py"}),
                ToolCall("must-not-finish", "finish", {"summary": "不能完成"}),
            )
            provider = StructuredScriptedProvider(
                [
                    _batch(*calls),
                    _batch(ToolCall("forbidden-next-round", "finish", {"summary": "不得再请求模型"})),
                ]
            )
            runtime, registry = _runtime_with_provider(
                root,
                base / "state",
                provider,
                registry_type=IdentityConflictRegistry,
            )

            result = runtime.run_task("应用补丁并处理身份冲突")

            self.assertFalse(result.ok)
            self.assertTrue(result.unknown_effects)
            self.assertEqual("文件影响未确认；请检查实际文件并通过 /clear 明确确认", result.summary)
            self.assertEqual(
                ("app.py",),
                result.modified_files,
                (result, runtime.current.context, runtime.current.journal.latest()),
            )
            self.assertEqual(["conflict"], registry.call_ids)
            self.assertEqual(1, len(provider.histories), "UNKNOWN 后不得再调用模型")
            self.assertEqual("external = True\n", first.read_text(encoding="utf-8"))
            self.assertEqual("value = 1\n", second.read_text(encoding="utf-8"))
            self.assertTrue(fault_state.get("external_replaced"))
            self.assertNotIn("unsafe_overwrite", fault_state)
            memory = runtime.store.load_memory(runtime.current.record.id)
            self.assertTrue(memory.unknown_effects)
            self.assertEqual(("app.py",), memory.modified_files)

            # /status 与 /diff 只读取 Runtime 本地状态；前者显示需核对，后者点名冲突路径。
            ui = FakeUI([])
            shell = InteractiveShell(runtime, ui)
            shell.execute("/status")
            shell.execute("/diff")
            rendered = "\n".join(ui.text)
            self.assertIn("文件影响未确认", rendered)
            self.assertIn("app.py", rendered)
            self.assertEqual([], ui.run_results)

            tool_messages = [message for message in runtime.current.context.messages if message.role == "tool"]
            self.assertEqual({call.id for call in calls}, {message.tool_call_id for message in tool_messages})
            self.assertEqual(len(calls), len(tool_messages))
            skipped = [
                json.loads(message.content or "{}")["tool_result"]["error"]["code"]
                for message in tool_messages[1:]
            ]
            self.assertEqual(["skipped", "skipped"], skipped)

            model_calls = len(provider.histories)
            self.assertFalse(runtime.run_task("未核对前不得继续").ok)
            self.assertEqual(model_calls, len(provider.histories))
            with self.assertRaises(SessionRuntimeError):
                runtime.undo_latest()
            database = runtime.store.database_path.read_bytes()
            self.assertIn(b"app.py", database)
            self.assertNotIn(b"external = True", database)

            class ForgedExternalEffects(ToolHandler):
                name = "external_forgery"
                description = "synthetic"
                parameters = ToolHandler._schema({})

                def run(self, arguments):  # type: ignore[no-untyped-def]
                    return ToolResult(
                        False,
                        "forged",
                        modified_paths=("fake.py",),
                        file_effects=FileEffects(EffectState.UNKNOWN, ("fake.py",)),
                    )

            registry.register(
                ForgedExternalEffects(registry.context),
                origin=ToolOrigin("mcp", "synthetic", "write"),
            )
            forged = registry.execute("external_forgery", {})
            self.assertEqual(FileEffects(EffectState.UNKNOWN), forged.file_effects)
            self.assertEqual((), forged.modified_paths)

    def test_i03_cancel_or_window_close_releases_waiting_approval(self) -> None:
        """真实 Runtime 等待审批时，取消与窗口关闭都不得启动命令或占住任务锁。"""
        for scenario in ("cancel", "window-close"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as raw:
                base = Path(raw).resolve()
                root = base / "workspace"
                root.mkdir()
                (root / "wait.py").write_text("print('must not start')\n", encoding="utf-8")
                entered = threading.Event()
                approvals: list[ApprovalWait] = []
                command_starts: list[object] = []
                results: list[object] = []
                errors: list[BaseException] = []

                class RecordingApprovalWait(ApprovalWait):
                    def __init__(self) -> None:
                        super().__init__()
                        approvals.append(self)

                class Delegate:
                    app: TricoderApp | None = None

                    def __call__(self, action: str, detail: str) -> bool:
                        assert self.app is not None
                        return self.app._approver(action, detail)

                delegate = Delegate()
                provider = StructuredScriptedProvider(
                    [
                        _batch(
                            ToolCall(
                                f"approval-{scenario}",
                                "run_command",
                                {"command": "python wait.py"},
                            )
                        )
                    ]
                )
                runtime, registry = _runtime_with_provider(
                    root,
                    base / "state",
                    provider,
                    approver=delegate,
                )
                app = TricoderApp(lambda *_: runtime)
                app.runtime = runtime
                delegate.app = app

                def record_ui_worker(_ask, **_kwargs):  # type: ignore[no-untyped-def]
                    # 不替换审批等待本身，只让虚构屏幕保持未决，直到真实取消入口关闭它。
                    entered.set()
                    return None

                def run_task() -> None:
                    try:
                        results.append(runtime.run_task(f"等待审批：{scenario}"))
                    except BaseException as exc:
                        errors.append(exc)

                worker = threading.Thread(target=run_task)
                with patch("tricoder.tui.ApprovalWait", RecordingApprovalWait), \
                        patch.object(app, "call_from_thread", side_effect=lambda callback: callback()), \
                        patch.object(app, "run_worker", side_effect=record_ui_worker), \
                        patch.object(app, "log_line"), \
                        patch("tricoder.tools.command.run_bounded_process") as run_process:
                    try:
                        worker.start()
                        self.assertTrue(entered.wait(2), "审批请求未进入等待")
                        if scenario == "cancel":
                            app.action_cancel()
                        else:
                            app.on_unmount()
                        worker.join(3)
                    finally:
                        if worker.is_alive():
                            runtime.cancel_current()
                            app._close_approvals()
                            worker.join(3)

                self.assertFalse(worker.is_alive(), "审批等待未退出")
                self.assertEqual([], errors)
                self.assertEqual(1, len(results))
                self.assertFalse(results[0].ok)  # type: ignore[union-attr]
                self.assertIn("取消", results[0].summary)  # type: ignore[union-attr]
                self.assertFalse(results[0].unknown_effects)  # type: ignore[union-attr]
                self.assertEqual([f"approval-{scenario}"], registry.call_ids)
                run_process.assert_not_called()
                self.assertEqual(1, len(approvals))
                self.assertFalse(approvals[0].resolve(True), "迟到批准不得覆盖取消终态")
                self.assertFalse(approvals[0].close(), "审批只能有一个终态")
                self.assertIsNone(runtime.current_task_cancellation())
                self.assertTrue(runtime.cleanup_pending_resources(), "公开清理入口应能取得已释放的任务锁")

    def test_i04_cancelled_process_keeps_primary_and_cleanup_owner(self) -> None:
        """真实受管进程取消后，清理失败保持独立证据并阻断资源复用。"""
        from tricoder import subprocess_control as control
        from tricoder.core.cancellation import CancellationError

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "workspace"
            root.mkdir()
            (root / "slow.py").write_text(
                "from pathlib import Path\n"
                "import time\n"
                "Path('started.txt').write_text('started', encoding='utf-8')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            provider = StructuredScriptedProvider(
                [
                    _batch(ToolCall("slow-command", "run_command", {"command": "python slow.py"})),
                    _batch(
                        ToolCall(
                            "after-cleanup-check",
                            "run_command",
                            {"command": "python -m compileall -q slow.py"},
                        ),
                        ToolCall("after-cleanup", "finish", {"summary": "资源已可复用"}),
                    ),
                ]
            )
            captured: list[CancellationError] = []

            class RecordingCancellationRegistry(CallIdRecordingRegistry):
                async def execute_async(self, name, arguments, **kwargs):  # type: ignore[no-untyped-def]
                    try:
                        return await super().execute_async(name, arguments, **kwargs)
                    except CancellationError as exc:
                        captured.append(exc)
                        raise

            runtime, registry = _runtime_with_provider(
                root,
                base / "state",
                provider,
                registry_type=RecordingCancellationRegistry,
            )
            original_cleanup = control._ProcessResources.cleanup
            allow_cleanup = threading.Event()
            resources: list[control._ProcessResources] = []

            def cleanup_then_report(resource, deadline):  # type: ignore[no-untyped-def]
                # 先做真实回收，确保测试不遗留进程；再模拟证据上无法确认清理完成。
                actually_clean = original_cleanup(resource, deadline)
                resources.append(resource)
                return actually_clean and allow_cleanup.is_set()

            results: list[object] = []
            errors: list[BaseException] = []

            def run_task() -> None:
                try:
                    results.append(runtime.run_task("运行受控长命令"))
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run_task)
            with patch.object(control._ProcessResources, "cleanup", cleanup_then_report):
                try:
                    worker.start()
                    deadline = time.monotonic() + 5
                    while not (root / "started.txt").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((root / "started.txt").exists(), "真实子进程没有启动")
                    self.assertTrue(runtime.cancel_current())
                    worker.join(5)
                    self.assertFalse(worker.is_alive(), "取消后的受管进程任务没有返回")
                    self.assertEqual([], errors)
                    self.assertEqual(1, len(results))
                    result = results[0]
                    self.assertFalse(result.ok)  # type: ignore[union-attr]
                    self.assertTrue(result.cleanup_failed)  # type: ignore[union-attr]
                    self.assertIn("取消", result.summary)  # type: ignore[union-attr]
                    self.assertIn("文件影响未确认", result.summary)  # type: ignore[union-attr]
                    self.assertTrue(result.unknown_effects)  # type: ignore[union-attr]
                    self.assertEqual(["slow-command"], registry.call_ids)
                    self.assertEqual(1, len(captured))
                    self.assertTrue(captured[0].cleanup_failed)
                    self.assertIn("取消", str(captured[0]))
                    self.assertTrue(resources)
                    self.assertTrue(all(resource.process.poll() is not None for resource in resources))

                    with self.assertRaises(SessionRuntimeError):
                        runtime.run_task("资源仍占用时不得开始下一任务")
                    self.assertFalse(runtime.cleanup_pending_resources())
                    allow_cleanup.set()
                    self.assertTrue(runtime.cleanup_pending_resources())

                    # 资源已经可复用；取消过已启动脚本产生的 UNKNOWN 仍需用户明确核对。
                    if runtime.current.memory.unknown_effects:
                        runtime.clear_current(confirmed=True)
                    self.assertTrue(runtime.run_task("显式清理后可复用").ok)
                finally:
                    allow_cleanup.set()
                    runtime.cancel_current()
                    runtime.cleanup_pending_resources()
                    worker.join(3)

            self.assertTrue(all(resource.process.poll() is not None for resource in resources))

    def test_i05_external_test_change_requires_new_version_evidence(self) -> None:
        """本地检查通过后外改测试文件，旧证据必须拒绝，重验只签发新版本。"""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "workspace"
            root.mkdir()
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            test_file = root / "test_app.py"
            test_file.write_text("assert True\n", encoding="utf-8")
            check = ToolCall(
                "check-v1",
                "run_command",
                {"command": "python -m compileall -q app.py test_app.py"},
            )
            provider = StructuredScriptedProvider(
                [
                    _batch(check, ToolCall("finish-v1", "finish", {"summary": "版本一已检查"})),
                    _batch(ToolCall("stale-finish", "finish", {"summary": "错误复用旧证据"})),
                    _batch(
                        ToolCall(
                            "check-v2",
                            "run_command",
                            {"command": "python -m compileall -q app.py test_app.py"},
                        ),
                        ToolCall("finish-v2", "finish", {"summary": "版本二已检查"}),
                    ),
                ]
            )
            runtime, registry = _runtime_with_provider(root, base / "state", provider)

            first = runtime.run_task("检查版本一")
            self.assertTrue(first.ok, (first, runtime.current.context, registry.call_ids))
            old_evidence = runtime.current.context.verification_evidence
            self.assertIsNotNone(old_evidence)
            self.assertTrue(registry.context.verification_scope.owns(old_evidence))

            test_file.write_text("assert 1 + 1 == 2\n", encoding="utf-8")
            stale = runtime.run_task("文件已外改但仅请求完成")
            self.assertFalse(stale.ok)
            self.assertEqual("待验证", stale.verification)
            self.assertIsNone(runtime.current.context.verification_evidence)
            current_snapshot = registry.context.verification_scope.capture(
                registry.context.workspace_policy
            )
            self.assertFalse(old_evidence.is_valid_for(current_snapshot))

            fresh = runtime.run_task("按新文件版本重新检查")
            self.assertTrue(fresh.ok)
            new_evidence = runtime.current.context.verification_evidence
            self.assertIsNotNone(new_evidence)
            self.assertTrue(registry.context.verification_scope.owns(new_evidence))
            self.assertNotEqual(old_evidence.after.digest, new_evidence.after.digest)
            self.assertEqual(old_evidence.after.scope_id, new_evidence.after.scope_id)
            self.assertEqual(
                ["check-v1", "finish-v1", "stale-finish", "check-v2", "finish-v2"],
                registry.call_ids,
            )

    def test_i06_session_reuse_isolation_restart_and_undo_boundaries(self) -> None:
        """四条真实 Runtime 路径分别证明证据复用与失效边界。"""
        for scenario in ("same-session", "separate-session", "restart", "undo"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as raw:
                base = Path(raw).resolve()
                root = base / "workspace"
                root.mkdir()
                (root / "app.py").write_text("x = 1\n", encoding="utf-8")
                state_dir = base / "state"

                if scenario == "same-session":
                    provider = StructuredScriptedProvider(
                        [
                            _batch(
                                ToolCall("same-check", "run_command", {"command": "python -m compileall -q app.py"}),
                                ToolCall("same-finish-1", "finish", {"summary": "首轮完成"}),
                            ),
                            _batch(ToolCall("same-finish-2", "finish", {"summary": "后续轮复用"})),
                        ]
                    )
                    runtime, registry = _runtime_with_provider(root, state_dir, provider)
                    self.assertTrue(runtime.run_task("同 Session 建立证据").ok)
                    evidence = runtime.current.context.verification_evidence
                    self.assertTrue(registry.context.verification_scope.owns(evidence))
                    self.assertTrue(runtime.run_task("同 Session 后续轮复核").ok)
                    self.assertIs(evidence, runtime.current.context.verification_evidence)

                elif scenario == "separate-session":
                    provider = StructuredScriptedProvider(
                        [
                            _batch(
                                ToolCall("first-check", "run_command", {"command": "python -m compileall -q app.py"}),
                                ToolCall("first-finish", "finish", {"summary": "第一会话"}),
                            ),
                            _batch(ToolCall("second-finish", "finish", {"summary": "第二会话纯读"})),
                            _batch(ToolCall("back-finish", "finish", {"summary": "切回不得复用"})),
                        ]
                    )
                    runtime, _registry = _runtime_with_provider(root, state_dir, provider)
                    self.assertTrue(runtime.run_task("第一会话检查").ok)
                    first_id = runtime.current.record.id
                    first_evidence = runtime.current.context.verification_evidence
                    runtime.create("second")
                    self.assertIsNone(runtime.current.context.verification_evidence)
                    second = runtime.run_task("第二会话纯读取完成")
                    self.assertTrue(second.ok)
                    self.assertEqual("未运行", second.verification)
                    runtime.switch(first_id, confirm=lambda _workspace: True)
                    self.assertIsNone(runtime.current.context.verification_evidence)
                    self.assertIsNot(first_evidence, runtime.current.context.verification_evidence)
                    self.assertFalse(runtime.run_task("切回后不得复用旧能力").ok)

                elif scenario == "restart":
                    provider = StructuredScriptedProvider(
                        [
                            _batch(
                                ToolCall("restart-check", "run_command", {"command": "python -m compileall -q app.py"}),
                                ToolCall("restart-finish", "finish", {"summary": "重启前"}),
                            )
                        ]
                    )
                    runtime, _registry = _runtime_with_provider(root, state_dir, provider)
                    self.assertTrue(runtime.run_task("重启前建立证据").ok)
                    old = runtime.current.context.verification_evidence
                    restarted, _ = _runtime_with_provider(
                        root,
                        state_dir,
                        StructuredScriptedProvider(
                            [_batch(ToolCall("restart-stale", "finish", {"summary": "不得恢复证据"}))]
                        ),
                    )
                    self.assertIsNone(restarted.current.context.verification_evidence)
                    self.assertEqual("待验证", restarted.current.context.verification)
                    self.assertFalse(restarted.run_task("重启后不得仅 finish").ok)
                    self.assertIsNot(old, restarted.current.context.verification_evidence)

                else:
                    provider = StructuredScriptedProvider(
                        [
                            _batch(
                                ToolCall(
                                    "undo-edit",
                                    "edit_file",
                                    {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
                                ),
                                ToolCall("undo-check", "run_command", {"command": "python -m compileall -q app.py"}),
                                ToolCall("undo-finish", "finish", {"summary": "修改已检查"}),
                            ),
                            _batch(ToolCall("undo-stale", "finish", {"summary": "撤销后不得复用"})),
                        ]
                    )
                    runtime, _registry = _runtime_with_provider(root, state_dir, provider)
                    self.assertTrue(runtime.run_task("修改并检查").ok)
                    old = runtime.current.context.verification_evidence
                    self.assertTrue(runtime.undo_latest().ok)
                    self.assertEqual("x = 1\n", (root / "app.py").read_text(encoding="utf-8"))
                    self.assertIsNone(runtime.current.context.verification_evidence)
                    self.assertEqual("待验证", runtime.current.context.verification)
                    self.assertFalse(runtime.run_task("撤销后仅 finish").ok)
                    self.assertIsNot(old, runtime.current.context.verification_evidence)

    def test_i07_native_and_legacy_protocols_pair_or_reject_every_call(self) -> None:
        """原生批次和 legacy 单动作都保持完整配对；重复原生 ID 在执行前拒绝。"""
        for scenario in ("native", "legacy", "duplicate-native"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as raw:
                base = Path(raw).resolve()
                root = base / "workspace"
                root.mkdir()
                (root / "app.py").write_text("x = 1\n", encoding="utf-8")

                if scenario == "native":
                    first_calls = (
                        ToolCall("native-bad", "create_file", {}),
                        ToolCall("native-skipped-read", "read_file", {"path": "app.py"}),
                        ToolCall("native-skipped-finish", "finish", {"summary": "不得执行"}),
                    )
                    recovery_calls = (
                        ToolCall("native-read", "read_file", {"path": "app.py"}),
                        ToolCall("native-finish", "finish", {"summary": "恢复完成"}),
                    )
                    provider = StructuredScriptedProvider(
                        [_batch(*first_calls), _batch(*recovery_calls)]
                    )
                    runtime, registry = _runtime_with_provider(root, base / "state", provider)
                    result = runtime.run_task("原生失败后重规划")
                    self.assertTrue(result.ok)
                    self.assertEqual(
                        ["native-bad", "native-read", "native-finish"],
                        registry.call_ids,
                        "skipped 调用不得到达 Registry",
                    )
                    issued = [call.id for call in (*first_calls, *recovery_calls)]
                    tool_messages = [
                        message for message in runtime.current.context.messages if message.role == "tool"
                    ]
                    result_ids = [message.tool_call_id for message in tool_messages]
                    self.assertTrue(all(issued))
                    self.assertEqual(len(issued), len(set(issued)))
                    self.assertEqual(issued, result_ids)
                    payloads = {
                        message.tool_call_id: json.loads(message.content or "{}")["tool_result"]
                        for message in tool_messages
                    }
                    self.assertEqual("skipped", payloads["native-skipped-read"]["error"]["code"])
                    self.assertEqual("skipped", payloads["native-skipped-finish"]["error"]["code"])

                elif scenario == "legacy":
                    provider = ScriptedProvider(
                        [
                            '{"tool":"create_file","arguments":{},"reason":"缺少路径"}',
                            '{"tool":"read_file","arguments":{"path":"app.py"},"reason":"恢复读取"}',
                            '{"tool":"finish","arguments":{"summary":"legacy 完成"},"reason":"结束"}',
                        ]
                    )
                    runtime, registry = _runtime_with_provider(
                        root,
                        base / "state",
                        provider,
                        tool_protocol="legacy_json",
                    )
                    result = runtime.run_task("legacy 失败后重规划")
                    self.assertTrue(result.ok)
                    self.assertEqual(3, len(registry.call_ids))
                    self.assertTrue(all(registry.call_ids))
                    self.assertEqual(len(registry.call_ids), len(set(registry.call_ids)))
                    messages = runtime.current.context.messages
                    rounds = [
                        (messages[index], messages[index + 1])
                        for index in range(len(messages) - 1)
                        if messages[index].role == "assistant"
                    ]
                    self.assertEqual(3, len(rounds))
                    self.assertTrue(
                        all(LegacyJsonProtocol().complete_round(assistant, tool) for assistant, tool in rounds)
                    )
                    self.assertEqual(
                        len(registry.call_ids),
                        sum(1 for message in messages if message.kind == "tool_result"),
                    )

                else:
                    provider = StructuredScriptedProvider(
                        [
                            _batch(
                                ToolCall("duplicate", "read_file", {"path": "app.py"}),
                                ToolCall("duplicate", "finish", {"summary": "不得执行"}),
                            )
                        ]
                    )
                    runtime, registry = _runtime_with_provider(
                        root,
                        base / "state",
                        provider,
                        max_rounds=1,
                    )
                    result = runtime.run_task("拒绝重复 call id")
                    self.assertFalse(result.ok)
                    self.assertEqual([], registry.call_ids)
                    self.assertEqual(
                        [],
                        [message for message in runtime.current.context.messages if message.kind == "tool_result"],
                    )
                    self.assertEqual(1, len(provider.histories))


if __name__ == "__main__":
    unittest.main()
