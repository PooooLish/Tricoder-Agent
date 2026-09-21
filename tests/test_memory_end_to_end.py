from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tricoder.agent import CodingAgent
from tricoder.context.memory import ConversationMemory, MemoryItem
from tricoder.context.summarizer import MemorySummaryResult
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    ProviderResponse,
    SessionContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tricoder.session_runtime import ActiveSession, RuntimeOptions, SessionRuntime
from tricoder.sessions import SessionStore


class _FinishProvider:
    """只结束业务任务的确定性 Provider；不访问网络。"""

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    f"finish-{self.provider_name}-{self.calls}",
                    "finish",
                    {"summary": "合成任务完成"},
                ),
            ),
            finish_reason="tool_calls",
        )


class _FinishTools:
    definitions = (ToolDefinition("finish", "结束", {"type": "object"}),)

    @staticmethod
    def contains(name: str) -> bool:
        return name == "finish"

    @staticmethod
    def describe(name: str):  # type: ignore[no-untyped-def]
        return _FinishTools.definitions[0] if name == "finish" else None

    @staticmethod
    def requires_approval(name: str) -> bool:
        return False

    @staticmethod
    def execute(name: str, arguments: dict[str, object], **kwargs):  # type: ignore[no-untyped-def]
        return ToolResult(True, str(arguments.get("summary", "")))


class _LifecycleSummarizer:
    """按任务 A/B 产生目标、修正约束和生命周期更新。"""

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        self.calls += 1
        task = next(message for message in source if message.kind == "task")
        source_id = f"m{task.message_seq}"
        covered = max(message.message_seq or 0 for message in source)
        if "任务 A" in (task.content or ""):
            candidate = ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=covered,
                goal=MemoryItem(
                    "goal-a", "完成任务 A", (source_id,), "task", task.task_id,
                ),
                constraints=(
                    MemoryItem("api-rule", "不得调整公共接口", (source_id,), "session"),
                ),
                decisions=(
                    MemoryItem("design-a", "使用方案 A", (source_id,), "session"),
                ),
                open_items=(
                    MemoryItem(
                        "todo-test", "补生命周期测试", (source_id,), "session",
                        state="pending",
                    ),
                ),
            )
        else:
            candidate = ConversationMemory(
                revision=previous.revision,
                generation=previous.generation,
                covered_through=covered,
                goal=MemoryItem(
                    "goal-b", "完成任务 B", (source_id,), "task", task.task_id,
                ),
                constraints=(
                    MemoryItem("api-rule", "允许按新需求调整公共接口", (source_id,), "session"),
                ),
                decisions=(
                    MemoryItem(
                        "design-b", "使用方案 B", (source_id,), "session",
                        replaces_id="design-a",
                    ),
                ),
                open_items=(
                    MemoryItem(
                        "todo-test", "补生命周期测试", (source_id,), "session",
                        state="done",
                    ),
                ),
            )
        return MemorySummaryResult(candidate, None)


class SessionMemoryEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        ids = iter(("session-main", "session-isolated"))
        self.store = SessionStore(self.database, id_factory=lambda: next(ids))
        self.store.initialize(self.workspace)
        self.main_record = self.store.create(
            "main", self.workspace, "openai", "openai-test"
        )
        self.summarizer = _LifecycleSummarizer()
        self.providers: list[_FinishProvider] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(self, provider: str) -> AppConfig:
        return AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                provider,
                "synthetic-test-key",
                "https://example.invalid/v1",
                f"{provider}-test",
            ),
            audit_dir=(self.root / "audit").resolve(),
            memory=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
        )

    def _factory(self, record, memory, options):  # type: ignore[no-untyped-def]
        provider = _FinishProvider(record.provider)
        self.providers.append(provider)
        config = self._config(record.provider)
        agent = CodingAgent(
            provider,
            _FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=config.memory,
            memory_summarizer=self.summarizer,
        )
        return ActiveSession(
            record,
            memory,
            SessionContext(persisted_summary=memory.summary),
            config,
            agent,
        )

    def _runtime(self, store: SessionStore) -> SessionRuntime:
        def load_config(**kwargs):  # type: ignore[no-untyped-def]
            return self._config(kwargs["provider"])

        return SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=self._factory,
            config_loader=load_config,
        )

    def test_two_tasks_model_switch_save_restart_isolation_and_clear(self) -> None:
        runtime = self._runtime(self.store)

        first = runtime.run_task("任务 A：保持公共接口并记录后续测试")
        second = runtime.run_task("任务 B：用户明确允许调整接口，采用方案 B 并完成测试待办")

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(2, self.summarizer.calls)
        self.assertEqual(2, sum(provider.calls for provider in self.providers))
        self.assertEqual(ConversationMemory(), runtime.current.context.conversation_memory)
        candidate = runtime.current.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        assert isinstance(candidate, ConversationMemory)
        self.assertEqual("goal-b", candidate.goal.id if candidate.goal else None)
        self.assertEqual(
            ("允许按新需求调整公共接口",),
            tuple(item.text for item in candidate.constraints),
        )
        self.assertEqual(("design-b",), tuple(item.id for item in candidate.decisions))
        self.assertEqual((), candidate.open_items)
        self.assertEqual(
            {("goal", "goal-a"), ("decisions", "design-a"), ("open_items", "todo-test")},
            {(entry.section, entry.item.id) for entry in candidate.archived},
        )
        self.assertEqual("strict", runtime.current.memory.permission_level)
        self.assertFalse(runtime.current.memory.unknown_effects)

        before_switch_context = runtime.current.context
        runtime.change_model("glm")
        self.assertEqual(before_switch_context, runtime.current.context)
        self.assertEqual("glm", runtime.current.record.provider)

        preview = runtime.preview_memory_save()
        self.assertEqual(candidate, preview.candidate)
        runtime.save_memory_preview(preview)
        self.assertEqual(
            (candidate, runtime.current.context.next_message_seq),
            runtime.store.load_conversation_memory(self.main_record.id),
        )

        restarted_store = SessionStore(
            self.database, id_factory=lambda: "session-isolated"
        )
        restarted = self._runtime(restarted_store)
        self.assertEqual(candidate, restarted.current.context.conversation_memory)
        self.assertIsNone(restarted.current.context.review_memory_candidate)
        self.assertEqual((), restarted.current.context.messages)

        isolated = restarted.create("isolated")
        self.assertEqual(ConversationMemory(), isolated.context.conversation_memory)
        restarted.switch(self.main_record.id, confirm=lambda _workspace: True)
        self.assertEqual(candidate, restarted.current.context.conversation_memory)

        old_generation = candidate.generation
        restarted.clear_current(confirmed=True)
        self.assertEqual(
            old_generation + 1,
            restarted.current.context.conversation_memory.generation,
        )
        self.assertIsNone(
            restarted.store.load_conversation_memory(self.main_record.id)
        )
        restarted.switch(isolated.record.id, confirm=lambda _workspace: True)
        self.assertEqual(
            ConversationMemory(), restarted.current.context.conversation_memory
        )


if __name__ == "__main__":
    unittest.main()
