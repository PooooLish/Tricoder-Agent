"""持久化工作区基线独立复审 F1/F2 的正式回归。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tricoder.models import (
    AppConfig,
    MemoryConfig,
    Message,
    ProviderConfig,
    ProviderResponse,
    SessionMemory,
    ToolCall,
)
from tricoder.presentation.shell import InteractiveShell
from tricoder.session.runtime import RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore
from tricoder.workspace.gate import WorkspaceGatePreview


class _RecordingProvider:
    """只返回合成响应，并保留实际进入 Provider 的消息视图。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.inputs: list[tuple[Message, ...]] = []

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.inputs.append(tuple(messages))
        if not self._responses:
            raise AssertionError("Provider 响应队列已耗尽")
        return self._responses.pop(0)


def _finish(call_id: str) -> ProviderResponse:
    return ProviderResponse(
        tool_calls=(
            ToolCall(
                call_id,
                "finish",
                {"summary": "已完成只读回顾", "outcome": "completed"},
            ),
        ),
        finish_reason="tool_calls",
    )


class _ShellUI:
    """Shell 集成测试所需的最小可观测界面。"""

    def __init__(self, selected_session_id: str) -> None:
        self.selected_session_id = selected_session_id
        self.notices: list[str] = []
        self.errors: list[str] = []

    def choose_session(self, _sessions, _current_id):  # type: ignore[no-untyped-def]
        return self.selected_session_id

    def confirm(self, _prompt: str) -> bool:
        return True

    def show_notice(self, message: str) -> None:
        self.notices.append(message)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append(f"{title}: {message}")

    def show_run_result(self, _result) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("损坏基线下不应产生任务结果")


class WorkspaceBaselineReviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.database = (self.root / "state" / "sessions.db").resolve()
        self.store = SessionStore(self.database)
        self.store.initialize(self.workspace)
        self.record = self.store.create(
            "first", self.workspace, "openai", "synthetic-model"
        )
        self._runtimes: list[SessionRuntime] = []
        self.addCleanup(self._close_runtimes)

    def _close_runtimes(self) -> None:
        for runtime in reversed(self._runtimes):
            runtime.close()

    def _config(self) -> AppConfig:
        return AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                "openai",
                "synthetic-test-key",
                "https://example.test/v1",
                "synthetic-model",
            ),
            audit_dir=(self.root / "state" / "audit").resolve(),
            plan_enabled=False,
            memory=MemoryConfig(compaction="off", persistence="off"),
        )

    def _runtime(
        self,
        session_id: str,
        provider: _RecordingProvider,
        *,
        confirmer,
    ) -> SessionRuntime:  # type: ignore[no-untyped-def]
        runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=lambda **_kwargs: self._config(),
            provider_factory=lambda _config, _timeout: provider,
            initial_session_id=session_id,
            approver=lambda _action, _detail: True,
            workspace_confirmer=confirmer,
        )
        self._runtimes.append(runtime)
        return runtime

    def _seed(self, session_id: str) -> None:
        runtime = self._runtime(
            session_id,
            _RecordingProvider([]),
            confirmer=lambda _preview: self.fail("首次扫描不应请求确认"),
        )
        self.assertTrue(runtime.close())
        self._runtimes.remove(runtime)

    def _corrupt_kind(self, session_id: str, value: object) -> tuple[str, int]:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            row = connection.execute(
                "SELECT payload_json, revision FROM session_workspace_baselines "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload["entries"][0]["kind"] = value
            encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
            connection.execute(
                "UPDATE session_workspace_baselines SET payload_json = ? "
                "WHERE session_id = ?",
                (encoded, session_id),
            )
            return encoded, row[1]

    def _stored_payload(self, session_id: str) -> tuple[str, int]:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT payload_json, revision FROM session_workspace_baselines "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0], row[1]

    @staticmethod
    def _provider_received_change_notice(provider: _RecordingProvider) -> bool:
        return any(
            message.kind == "workspace_change"
            and message.content is not None
            and "历史源码观察和实现状态可能过期" in message.content
            for request in provider.inputs
            for message in request
        )

    def test_restart_acceptance_notifies_first_provider_turn_without_second_prompt(self) -> None:
        """重启激活确认后，通知必须保留到下一次真实上下文装配。"""

        self.store.save_memory(
            self.record.id,
            SessionMemory(
                verification="failed",
                verification_obligation="legacy_unknown",
                pending_verification_paths=("old.py",),
            ),
        )
        self._seed(self.record.id)
        (self.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
        previews: list[WorkspaceGatePreview] = []
        provider = _RecordingProvider([_finish("restart-finish")])

        runtime = self._runtime(
            self.record.id,
            provider,
            confirmer=lambda preview: previews.append(preview) or True,
        )
        notice_before_task = runtime.current.context.workspace_change_notice
        result = runtime.run_task("继续只读回顾")

        self.assertTrue(result.ok, result.summary)
        self.assertIn("历史源码观察和实现状态可能过期", notice_before_task)
        self.assertTrue(self._provider_received_change_notice(provider))
        self.assertEqual(1, len(previews))
        self.assertEqual("legacy_unknown", runtime.current.memory.verification_obligation)
        self.assertEqual(
            {"old.py", "app.py"},
            set(runtime.current.memory.pending_verification_paths),
        )

    def test_switch_acceptance_notifies_first_provider_turn_without_second_prompt(self) -> None:
        """切换入口接受目标 Session 的变化后也必须只确认一次并通知 Agent。"""

        self.store.save_memory(
            self.record.id,
            SessionMemory(
                verification="failed",
                verification_obligation="legacy_unknown",
                pending_verification_paths=("old.py",),
            ),
        )
        self._seed(self.record.id)
        (self.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
        source = self.store.create(
            "source", self.workspace, "openai", "synthetic-model"
        )
        self._seed(source.id)
        previews: list[WorkspaceGatePreview] = []
        provider = _RecordingProvider([_finish("switch-finish")])
        runtime = self._runtime(
            source.id,
            provider,
            confirmer=lambda preview: previews.append(preview) or True,
        )

        runtime.switch(self.record.id, confirm=lambda _workspace: True)
        notice_before_task = runtime.current.context.workspace_change_notice
        result = runtime.run_task("读取切换后的最新代码")

        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.record.id, runtime.current.record.id)
        self.assertIn("历史源码观察和实现状态可能过期", notice_before_task)
        self.assertTrue(self._provider_received_change_notice(provider))
        self.assertEqual(1, len(previews))
        self.assertEqual("legacy_unknown", runtime.current.memory.verification_obligation)
        self.assertEqual(
            {"old.py", "app.py"},
            set(runtime.current.memory.pending_verification_paths),
        )

    def test_activation_rejection_never_publishes_notice_or_calls_provider(self) -> None:
        """拒绝确认只保留会话和旧基线，不能发布已确认变化的通知。"""

        self._seed(self.record.id)
        before = self.store.load_workspace_baseline(self.record.id)
        (self.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
        provider = _RecordingProvider([_finish("must-not-run")])
        runtime = self._runtime(
            self.record.id,
            provider,
            confirmer=lambda _preview: False,
        )

        self.assertEqual("", runtime.current.context.workspace_change_notice)
        with self.assertRaises(SessionRuntimeError):
            runtime.run_task("不得进入 Provider")

        self.assertEqual(0, provider.calls)
        self.assertEqual(before, self.store.load_workspace_baseline(self.record.id))
        self.assertEqual("", runtime.current.context.workspace_change_notice)

    def test_activation_storage_failure_never_publishes_confirmed_notice(self) -> None:
        """确认后的 CAS/存储失败也不能让下一轮误以为基线已经接受。"""

        self._seed(self.record.id)
        before = self.store.load_workspace_baseline(self.record.id)
        (self.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
        source = self.store.create(
            "source", self.workspace, "openai", "synthetic-model"
        )
        self._seed(source.id)
        previews: list[WorkspaceGatePreview] = []
        provider = _RecordingProvider([])
        runtime = self._runtime(
            source.id,
            provider,
            confirmer=lambda preview: previews.append(preview) or True,
        )
        original_save = runtime.store.save_workspace_baseline

        def fail_save(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError("synthetic storage failure")

        runtime.store.save_workspace_baseline = fail_save  # type: ignore[method-assign]
        try:
            runtime.switch(self.record.id, confirm=lambda _workspace: True)
        finally:
            runtime.store.save_workspace_baseline = original_save  # type: ignore[method-assign]

        self.assertEqual(self.record.id, runtime.current.record.id)
        self.assertEqual("error", runtime.status().workspace_baseline_state)
        self.assertEqual("", runtime.current.context.workspace_change_notice)
        self.assertEqual(1, len(previews))
        self.assertEqual(0, provider.calls)
        self.assertEqual(before, self.store.load_workspace_baseline(self.record.id))

    def test_corrupted_kind_runtime_restore_keeps_session_and_payload_unchanged(self) -> None:
        """Store→Runtime 恢复把类型损坏归一为 corrupted，并阻断 Provider。"""

        self._seed(self.record.id)
        before = self._corrupt_kind(self.record.id, [])
        provider = _RecordingProvider([_finish("must-not-run")])

        runtime = self._runtime(
            self.record.id,
            provider,
            confirmer=lambda _preview: True,
        )

        self.assertEqual(self.record.id, runtime.current.record.id)
        self.assertEqual("corrupted", runtime.status().workspace_baseline_state)
        with self.assertRaisesRegex(SessionRuntimeError, "历史基线不可用"):
            runtime.run_task("不得进入 Provider")
        self.assertEqual(0, provider.calls)
        self.assertEqual(before, self._stored_payload(self.record.id))

    def test_shell_switch_to_corrupted_session_keeps_selection_and_blocks_task(self) -> None:
        """Shell 切换到损坏 Session 后保留选择，但文件任务必须 fail-closed。"""

        self._seed(self.record.id)
        source = self.store.create(
            "source", self.workspace, "openai", "synthetic-model"
        )
        self._seed(source.id)
        before = self._corrupt_kind(self.record.id, {})
        provider = _RecordingProvider([_finish("must-not-run")])
        runtime = self._runtime(
            source.id,
            provider,
            confirmer=lambda _preview: True,
        )
        ui = _ShellUI(self.record.id)
        shell = InteractiveShell(runtime, ui)

        shell.execute("/session")
        shell.execute("不得进入 Provider")

        self.assertEqual(self.record.id, runtime.current.record.id)
        self.assertEqual("corrupted", runtime.status().workspace_baseline_state)
        self.assertEqual(0, provider.calls)
        self.assertEqual(before, self._stored_payload(self.record.id))
        self.assertTrue(any("历史基线不可用" in error for error in ui.errors))


if __name__ == "__main__":
    unittest.main()
