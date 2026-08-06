"""会话运行时与 SQLite 持久化边界的集成验证。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tricoder.agent import PLANNING_PROMPT
from tricoder.models import (
    AppConfig,
    Message,
    ProviderConfig,
    ProviderResponse,
    RunResult,
    SessionContext,
    SessionTurnResult,
    ToolCall,
    ToolDefinition,
)
from tricoder.session_runtime import ActiveSession, RuntimeOptions, SessionRuntime
from tricoder.sessions import SessionStore
from tricoder.agent import CodingAgent
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext, ToolRegistry


class RecordingAgent:
    """不访问网络，记录每个 Session 收到的运行时上下文。"""

    def __init__(self, provider: str, result_summaries: dict[str, str]) -> None:
        self.provider = provider
        self.result_summaries = result_summaries
        self.calls: list[tuple[str, SessionContext]] = []

    def run_with_context(self, task: str, context: SessionContext) -> SessionTurnResult:
        self.calls.append((task, context))
        call_id = f"{self.provider}-call-{len(self.calls)}"
        structured_round = (
            Message(
                "assistant",
                None,
                tool_calls=(ToolCall(call_id, "finish", {"summary": task}),),
            ),
            Message(
                "tool",
                '{"ok":true}',
                kind="tool_result",
                tool_call_id=call_id,
            ),
        )
        return SessionTurnResult(
            RunResult(
                ok=True,
                summary=self.result_summaries.get(task, f"{self.provider} verification completed"),
                rounds=1,
                modified_files=(f"src/{self.provider}.py",),
                verification="passed",
            ),
            SessionContext(
                messages=(
                    *context.messages,
                    Message("user", task, kind="task"),
                    *structured_round,
                ),
                persisted_summary=context.persisted_summary,
                modified_files=(f"src/{self.provider}.py",),
                verification="passed",
            ),
        )


class ScriptedProvider:
    """为真实 CodingAgent 提供本地固定动作序列。"""

    def __init__(
        self,
        responses: list[str],
        *,
        call_id_prefix: str = "integration-call",
    ) -> None:
        self.responses = list(responses)
        self.call_id_prefix = call_id_prefix
        self.call_number = 0
        self.histories: list[list[Message]] = []
        self.tool_batches: list[tuple[ToolDefinition, ...]] = []
        self.calls: list[ToolCall] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        if (
            not tools
            and messages
            and getattr(messages[-1], "content", None) == PLANNING_PROMPT
        ):
            return ProviderResponse(
                content='{"steps": ["步骤 1", "步骤 2", "步骤 3"]}'
            )
        self.histories.append(list(messages))
        self.tool_batches.append(tuple(tools))
        decoded = json.loads(self.responses.pop(0))
        self.call_number += 1
        call = ToolCall(
            f"{self.call_id_prefix}-{self.call_number}",
            decoded["tool"],
            decoded["arguments"],
        )
        self.calls.append(call)
        return ProviderResponse(tool_calls=(call,))


def action(tool: str, arguments: dict[str, object]) -> str:
    return json.dumps({"tool": tool, "arguments": arguments, "reason": "integration"})


@dataclass
class RecordingSessionFactory:
    """构建可观察的假会话，不读取 Key 或创建审计文件。"""

    agents: dict[str, RecordingAgent]
    result_summaries: dict[str, str]

    def __call__(self, record, memory, _options) -> ActiveSession:  # type: ignore[no-untyped-def]
        agent = RecordingAgent(record.provider, self.result_summaries)
        self.agents[record.id] = agent
        config = AppConfig(
            workspace=record.workspace,
            provider=ProviderConfig(record.provider, "test-key", "https://example.test", record.model),
        )
        return ActiveSession(
            record=record,
            memory=memory,
            context=SessionContext(
                persisted_summary=memory.summary,
                modified_files=memory.modified_files,
                verification=memory.verification,
            ),
            config=config,
            agent=agent,
        )


class SessionIntegrationTests(unittest.TestCase):
    def test_default_active_session_shares_one_ephemeral_journal_with_tools(self) -> None:
        """防止默认装配让 Runtime 与写工具记录到不同账本，或把快照写进 SQLite。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SessionStore((root / "state" / "sessions.db").resolve())
            registries: list[ToolRegistry] = []

            def config_loader(**kwargs):  # type: ignore[no-untyped-def]
                provider = kwargs["provider"]
                return AppConfig(
                    workspace=workspace,
                    provider=ProviderConfig(
                        provider,
                        "test-key",
                        "https://example.test",
                        "model-a",
                    ),
                    audit_dir=root / "audit",
                )

            def agent_factory(_provider, tools, **_kwargs):  # type: ignore[no-untyped-def]
                registries.append(tools)
                return RecordingAgent("openai", {})

            runtime = SessionRuntime(
                store,
                workspace,
                options=RuntimeOptions(environ={}),
                config_loader=config_loader,
                provider_factory=lambda _config, _timeout: object(),
                agent_factory=agent_factory,
            )

            self.assertIs(runtime.current.tools, registries[0])
            self.assertIs(
                runtime.current.journal,
                registries[0].context.change_journal,
            )
            self.assertIsNotNone(runtime.current.audit)
            self.assertIsNone(runtime.diff_latest())

    def test_structured_context_survives_model_and_session_switches_until_clear(
        self,
    ) -> None:
        """防止模型重建或会话切换串改结构化消息；清空只影响当前会话。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_workspace = (root / "workspace-one").resolve()
            second_workspace = (root / "workspace-two").resolve()
            first_workspace.mkdir()
            second_workspace.mkdir()
            store = SessionStore((root / "state" / "sessions.db").resolve())
            store.initialize(first_workspace)
            first = store.create("first", first_workspace, "openai", "model-a")
            second = store.create("second", second_workspace, "glm", "model-b")
            factory = RecordingSessionFactory({}, {})

            def load_config(
                *,
                provider: str,
                workspace: Path,
                model: str | None = None,
                **_options: object,
            ) -> AppConfig:
                return AppConfig(
                    workspace=workspace,
                    provider=ProviderConfig(
                        provider,
                        "test-key",
                        "https://example.test",
                        model or f"{provider}-model",
                    ),
                )

            runtime = SessionRuntime(
                store,
                first_workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=factory,
                config_loader=load_config,
            )
            runtime.run_task("first structured task")
            first_snapshot = runtime.current.context

            runtime.change_model("glm")

            self.assertEqual(first_snapshot, runtime.current.context)
            self.assertTrue(first_snapshot.messages[-2].tool_calls)
            self.assertEqual(
                first_snapshot.messages[-2].tool_calls[0].id,
                first_snapshot.messages[-1].tool_call_id,
            )

            runtime.switch(second.id, confirm=lambda _workspace: True)
            self.assertEqual((), runtime.current.context.messages)
            runtime.run_task("second structured task")
            second_snapshot = runtime.current.context
            self.assertNotEqual(first_snapshot.messages, second_snapshot.messages)

            runtime.switch(first.id, confirm=lambda _workspace: True)
            self.assertEqual(first_snapshot, runtime.current.context)
            runtime.clear_current()
            self.assertEqual((), runtime.current.context.messages)

            runtime.switch(second.id, confirm=lambda _workspace: True)
            self.assertEqual(second_snapshot, runtime.current.context)

    def test_sqlite_persists_only_canonical_relative_paths_after_agent_writes(self) -> None:
        """防止 Agent 写入后将绝对路径或 dotdot 形式保存到 SQLite。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            (workspace / "sub").mkdir()
            (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
            (workspace / "other.py").write_text("value = 1\n", encoding="utf-8")
            database = (root / "system-state" / "TriCoder" / "sessions.db").resolve()
            task_marker = "TASK-CONTEXT-MARKER-7A19"
            argument_marker = "ARGUMENT-CONTEXT-MARKER-7B20"
            call_id_marker = "CALL-ID-CONTEXT-MARKER-7C21"
            provider = ScriptedProvider(
                [
                    action(
                        "edit_file",
                        {
                            "path": str(workspace / "sample.py"),
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    ),
                    action(
                        "edit_file",
                        {
                            "path": "sub/../other.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    ),
                    action("run_command", {"command": "python -m compileall -q sample.py other.py"}),
                    action("finish", {"summary": argument_marker}),
                ],
                call_id_prefix=call_id_marker,
            )

            def active_factory(record, memory, _options):  # type: ignore[no-untyped-def]
                tools = ToolRegistry(
                    ToolContext(
                        WorkspacePolicy(workspace),
                        CommandPolicy(),
                        approver=lambda _action, _detail: True,
                    )
                )
                config = AppConfig(
                    workspace=workspace,
                    provider=ProviderConfig(record.provider, "test-key", "https://example.test", record.model),
                )
                return ActiveSession(
                    record,
                    memory,
                    SessionContext(persisted_summary=memory.summary),
                    config,
                    CodingAgent(provider, tools, max_rounds=5, plan_enabled=False),
                )

            store = SessionStore(database)
            runtime = SessionRuntime(
                store,
                workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=active_factory,
            )

            result = runtime.run_task(task_marker)

            self.assertTrue(result.ok)
            self.assertEqual(("sample.py", "other.py"), result.modified_files)
            histories = getattr(provider, "histories", [])
            tool_batches = getattr(provider, "tool_batches", [])
            calls = getattr(provider, "calls", [])
            self.assertEqual(4, len(histories))
            self.assertEqual(4, len(tool_batches))
            self.assertEqual(4, len(calls))
            self.assertTrue(all(tool_batches))
            registered_names = [
                {definition.name for definition in definitions}
                for definitions in tool_batches
            ]
            self.assertEqual(
                ["edit_file", "edit_file", "run_command", "finish"],
                [call.name for call in calls],
            )
            self.assertTrue(
                all(
                    expected in names
                    for expected, names in zip(
                        ("edit_file", "edit_file", "run_command", "finish"),
                        registered_names,
                    )
                )
            )
            self.assertEqual("system", histories[0][0].role)
            self.assertEqual("task", histories[0][-1].kind)
            for history in histories[1:]:
                with self.subTest(request_length=len(history)):
                    assistant, tool_result = history[-2:]
                    self.assertEqual("assistant", assistant.role)
                    self.assertEqual("tool", tool_result.role)
                    self.assertEqual(
                        assistant.tool_calls[0].id,
                        tool_result.tool_call_id,
                    )
            persisted = store.load_memory(runtime.current.record.id)
            self.assertEqual(("sample.py", "other.py"), persisted.modified_files)
            forbidden_markers = (task_marker, argument_marker, call_id_marker)
            persisted_text_fields = (
                persisted.summary,
                persisted.requirements_summary,
                persisted.last_task_summary,
                persisted.verification,
            )
            for marker in forbidden_markers:
                with self.subTest(marker=marker):
                    self.assertTrue(
                        all(marker not in value for value in persisted_text_fields)
                    )
            connection = sqlite3.connect(database)
            try:
                stored_paths = connection.execute(
                    "SELECT modified_files_json FROM session_memory WHERE session_id = ?",
                    (runtime.current.record.id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertNotIn(str(workspace), stored_paths)
            self.assertNotIn("..", stored_paths)
            database_text = database.read_bytes().decode("utf-8", errors="ignore")
            for marker in forbidden_markers:
                with self.subTest(database_marker=marker):
                    self.assertNotIn(marker, database_text)

    def test_sessions_are_isolated_restart_restores_safe_summary_and_workspace_stays_clean(self) -> None:
        """跨工作区切换不串状态，重启仅恢复安全摘要，SQLite 不写入工作区或原文。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_workspace = (root / "workspace-one").resolve()
            second_workspace = (root / "workspace-two").resolve()
            first_workspace.mkdir()
            second_workspace.mkdir()
            database = (root / "system-state" / "TriCoder" / "sessions.db").resolve()
            store = SessionStore(database)
            store.initialize(first_workspace)
            first = store.create("first", first_workspace, "openai", "model-a")
            second = store.create("second", second_workspace, "glm", "model-b")
            result_summaries = (
                "ls -la",
                "Get-ChildItem -Force",
                "echo note",
                "x = 1",
                "foo()",
                "测试完成",
                "verification completed",
            )
            first_tasks = tuple(f"record result {index}" for index in range(len(result_summaries)))
            factory = RecordingSessionFactory(
                {},
                dict(zip(first_tasks, result_summaries)),
            )
            runtime = SessionRuntime(
                store,
                first_workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=factory,
            )

            for task in first_tasks:
                runtime.run_task(task)
            first_context = runtime.current.context
            self.assertEqual("model-a", runtime.current.config.provider.model)

            runtime.switch(second.id, confirm=lambda _workspace: True)
            runtime.run_task("check second workspace only")

            self.assertEqual(second_workspace, runtime.current.config.workspace)
            self.assertEqual("glm", runtime.current.config.provider.name)
            self.assertEqual("model-b", runtime.current.config.provider.model)
            self.assertNotEqual(first_context, runtime.current.context)
            self.assertEqual(list(first_tasks), [task for task, _context in factory.agents[first.id].calls])
            self.assertEqual(
                ["check second workspace only"],
                [task for task, _context in factory.agents[second.id].calls],
            )
            self.assertEqual((), factory.agents[second.id].calls[0][1].messages)
            self.assertEqual(database, store.database_path)
            self.assertFalse(any(first_workspace.iterdir()))
            self.assertFalse(any(second_workspace.iterdir()))

            restarted_factory = RecordingSessionFactory({}, {})
            restarted = SessionRuntime(
                SessionStore(database),
                first_workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=restarted_factory,
            )

            self.assertEqual(first.id, restarted.current.record.id)
            self.assertEqual("openai", restarted.current.config.provider.name)
            self.assertEqual(first_workspace, restarted.current.config.workspace)
            self.assertEqual((), restarted.current.context.messages)
            self.assertEqual(
                store.load_memory(first.id).summary,
                restarted.current.context.persisted_summary,
            )
            self.assertEqual(
                "run: succeeded; modified_files=1; verification=passed",
                restarted.current.context.persisted_summary,
            )

            database_text = database.read_bytes().decode("utf-8", errors="ignore")
            for forbidden in result_summaries:
                with self.subTest(forbidden=forbidden[:12]):
                    self.assertNotIn(forbidden, database_text)


if __name__ == "__main__":
    unittest.main()
