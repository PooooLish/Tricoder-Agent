from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.context.memory import ConversationMemory, MemoryItem, MemoryValidationError
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    Message,
    ProviderConfig,
    SessionContext,
    SessionMemory,
    ToolCall,
)
from tricoder.protocols import NativeToolProtocol
from tricoder.session_runtime import ActiveSession, RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.sessions import SessionError, SessionStore


def block(start: int, label: str) -> tuple[Message, ...]:
    call = ToolCall(f"call-{label}", "read_file", {"path": f"{label}.txt"})
    task_id = f"task-{start}"
    return (
        Message("user", label, kind="task", message_seq=start, task_id=task_id),
        Message("assistant", None, tool_calls=(call,), message_seq=start + 1, task_id=task_id),
        Message(
            "tool", "ok", kind="tool_result", tool_call_id=call.id,
            message_seq=start + 2, task_id=task_id,
        ),
    )


class FlakyClearStore(SessionStore):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.fail_clear_once = False

    def clear_conversation_memory(self, session_id: str) -> None:
        if self.fail_clear_once:
            self.fail_clear_once = False
            raise SessionError("synthetic clear failure")
        super().clear_conversation_memory(session_id)


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runtime(self, store: SessionStore) -> SessionRuntime:
        config = AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig("openai", "test", "https://api.openai.com/v1", "test"),
            audit_dir=(self.root / "audit").resolve(),
            memory=MemoryConfig(compaction="structured", persistence="reviewed_summary"),
        )

        def factory(record, memory, options):  # type: ignore[no-untyped-def]
            return ActiveSession(
                record,
                memory,
                SessionContext(
                    persisted_summary=memory.summary,
                    modified_files=memory.modified_files,
                    verification=memory.verification,
                    unknown_effects=memory.unknown_effects,
                ),
                config,
                object(),
            )

        return SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=factory,
        )

    def _seed_store(self, store: SessionStore) -> str:
        store.initialize(self.workspace)
        record = store.create("one", self.workspace, "openai", "test")
        store.save_memory(
            record.id,
            SessionMemory(modified_files=("src/app.py",), verification="待验证"),
        )
        memory = ConversationMemory(
            revision=1,
            generation=4,
            covered_through=2,
            constraints=(MemoryItem("api", "不改公共接口", ("m1",), "session"),),
        )
        store.save_conversation_memory(
            record.id, memory, next_message_seq=3, expected_revision=None
        )
        return record.id

    def test_clear_increments_generation_preserves_execution_state_and_deletes_saved_memory(self) -> None:
        store = SessionStore(self.database, id_factory=lambda: "session-1")
        session_id = self._seed_store(store)
        runtime = self._runtime(store)
        runtime.current = replace(
            runtime.current,
            context=replace(runtime.current.context, messages=block(3, "recent"), next_message_seq=6),
        )

        runtime.clear_current(confirmed=True)

        context = runtime.current.context
        self.assertEqual(5, context.conversation_memory.generation)
        self.assertEqual((), context.messages)
        self.assertEqual(6, context.next_message_seq)
        self.assertEqual(("src/app.py",), context.modified_files)
        self.assertFalse(context.memory_pending_clear)
        self.assertIsNone(store.load_conversation_memory(session_id))

    def test_clear_database_failure_blocks_reload_and_save_until_local_retry(self) -> None:
        store = FlakyClearStore(self.database, id_factory=lambda: "session-1")
        self._seed_store(store)
        runtime = self._runtime(store)
        store.fail_clear_once = True

        with self.assertRaisesRegex(SessionRuntimeError, "持久化清除失败"):
            runtime.clear_current(confirmed=True)

        self.assertTrue(runtime.current.context.memory_pending_clear)
        self.assertEqual((), runtime.current.context.messages)
        with self.assertRaises(SessionRuntimeError):
            runtime.preview_memory_save()
        self.assertTrue(runtime.retry_persist())
        self.assertFalse(runtime.current.context.memory_pending_clear)
        self.assertIsNone(store.load_conversation_memory(runtime.current.record.id))

    def test_late_candidate_from_old_generation_cannot_revive_cleared_memory(self) -> None:
        messages = (*block(1, "old"), *block(4, "recent"))
        manager = ContextManager(ContextBudget(max_chars=120), NativeToolProtocol())
        plan = manager.plan_compaction(messages, trigger_ratio=0.8, target_ratio=0.6)
        self.assertTrue(plan.needs_compaction)
        cleared = SessionContext(
            messages=(),
            conversation_memory=ConversationMemory(generation=1),
            next_message_seq=7,
        )
        stale = ConversationMemory(
            generation=0,
            covered_through=plan.covered_through,
            constraints=(MemoryItem("old", "旧记忆", ("m1",), "session"),),
        )

        with self.assertRaises(MemoryValidationError):
            # 使用清除后的 generation 作为可信基线；旧候选即使迟到也不能提交。
            manager.commit_compaction(
                replace(cleared, messages=messages),
                plan,
                stale,
            )
        self.assertEqual(ConversationMemory(generation=1), cleared.conversation_memory)

    def test_fixed_long_history_comparison_preserves_constraint_across_two_compactions(self) -> None:
        history = (
            *block(1, "不改公共接口"),
            *block(4, "task-b"),
            *block(7, "task-c"),
            *block(10, "task-d"),
        )
        manager = ContextManager(ContextBudget(max_chars=180), NativeToolProtocol())
        legacy_view = manager.prepare(history)
        self.assertFalse(any(message.content == "不改公共接口" for message in legacy_view.messages))

        plan = manager.plan_compaction(history, trigger_ratio=0.8, target_ratio=0.6)
        first = manager.commit_compaction(
            SessionContext(messages=history, next_message_seq=13),
            plan,
            ConversationMemory(
                covered_through=plan.covered_through,
                constraints=(MemoryItem("public-api", "不改公共接口", ("m1",), "session"),),
            ),
        )
        extended = replace(
            first,
            messages=(*first.messages, *block(13, "task-e"), *block(16, "task-f")),
            next_message_seq=19,
        )
        second_plan = manager.plan_compaction(
            extended.messages,
            trigger_ratio=0.8,
            target_ratio=0.6,
            covered_through=first.conversation_memory.covered_through,
        )
        second = manager.commit_compaction(
            extended,
            second_plan,
            ConversationMemory(
                revision=first.conversation_memory.revision,
                generation=first.conversation_memory.generation,
                covered_through=second_plan.covered_through,
                constraints=(
                    MemoryItem("public-api", "允许修改公共接口", ("m13",), "session"),
                    MemoryItem("new", "不新增向量库", ("m13",), "session"),
                ),
            ),
        )

        self.assertEqual(
            ("不改公共接口", "不新增向量库"),
            tuple(item.text for item in second.conversation_memory.constraints),
        )
        self.assertLess(len(second.messages), len(extended.messages))


if __name__ == "__main__":
    unittest.main()
