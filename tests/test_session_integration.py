"""会话运行时与 SQLite 持久化边界的集成验证。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tricoder.models import AppConfig, Message, ProviderConfig, RunResult, SessionContext, SessionTurnResult
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
        return SessionTurnResult(
            RunResult(
                ok=True,
                summary=self.result_summaries.get(task, f"{self.provider} verification completed"),
                rounds=1,
                modified_files=(f"src/{self.provider}.py",),
                verification="passed",
            ),
            SessionContext(
                messages=context.messages + (Message("user", task, kind="task"),),
                persisted_summary=context.persisted_summary,
                modified_files=(f"src/{self.provider}.py",),
                verification="passed",
            ),
        )


class ScriptedProvider:
    """为真实 CodingAgent 提供本地固定动作序列。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def complete(self, _messages) -> str:  # type: ignore[no-untyped-def]
        return self.responses.pop(0)


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
                    action("finish", {"summary": "completed"}),
                ]
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
                    CodingAgent(provider, tools, max_rounds=5),
                )

            store = SessionStore(database)
            runtime = SessionRuntime(
                store,
                workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=active_factory,
            )

            result = runtime.run_task("canonicalize write metadata")

            self.assertTrue(result.ok)
            self.assertEqual(("sample.py", "other.py"), result.modified_files)
            persisted = store.load_memory(runtime.current.record.id)
            self.assertEqual(("sample.py", "other.py"), persisted.modified_files)
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
