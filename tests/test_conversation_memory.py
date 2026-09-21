from __future__ import annotations

import json
import unittest

from tricoder.agent import CodingAgent
from tricoder.context.memory import (
    ConversationMemory,
    MemoryItem,
    MemoryValidationError,
    assign_message_sequences,
    memory_from_json,
    memory_to_json,
    merge_candidate,
    validate_candidate,
)
from tricoder.models import (
    Message,
    ProviderResponse,
    SessionContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


def item(
    item_id: str,
    text: str,
    *sources: str,
    scope: str = "session",
    task_id: str | None = None,
    state: str = "active",
) -> MemoryItem:
    return MemoryItem(item_id, text, tuple(sources), scope, task_id, state=state)


class ConversationMemoryTests(unittest.TestCase):
    def test_json_round_trip_is_stable_and_strict(self) -> None:
        memory = ConversationMemory(
            revision=2,
            generation=3,
            covered_through=8,
            goal=item("goal", "完成会话记忆", "m1"),
            constraints=(item("constraint-api", "不改公共接口", "m2"),),
            decisions=(item("decision-db", "使用独立 SQLite 表", "m3"),),
            open_items=(
                item(
                    "todo-tests", "补充重启测试", "m8", scope="task",
                    task_id="task-7", state="pending",
                ),
            ),
        )

        encoded = memory_to_json(memory)
        restored = memory_from_json(encoded, allowed_source_ids={"m1", "m2", "m3", "m8"})

        self.assertEqual(memory, restored)
        self.assertEqual(encoded, memory_to_json(restored))

    def test_unknown_fields_versions_and_execution_state_are_rejected(self) -> None:
        base = json.loads(memory_to_json(ConversationMemory()))
        for field, value in (
            ("permission", "fullaccess"),
            ("verification", "passed"),
            ("unknown_effects", False),
        ):
            with self.subTest(field=field):
                payload = dict(base)
                payload[field] = value
                with self.assertRaises(MemoryValidationError):
                    memory_from_json(json.dumps(payload), allowed_source_ids=set())

        base["schema_version"] = 99
        with self.assertRaises(MemoryValidationError):
            memory_from_json(json.dumps(base), allowed_source_ids=set())

    def test_forged_source_and_limits_are_rejected_without_truncation(self) -> None:
        forged = ConversationMemory(goal=item("goal", "目标", "m404"))
        with self.assertRaises(MemoryValidationError):
            validate_candidate(forged, allowed_source_ids={"m1"})

        too_long = ConversationMemory(goal=item("goal", "x" * 501, "m1"))
        with self.assertRaises(MemoryValidationError):
            validate_candidate(too_long, allowed_source_ids={"m1"})

        too_many = ConversationMemory(
            constraints=tuple(item(f"c{i}", str(i), "m1") for i in range(21))
        )
        with self.assertRaises(MemoryValidationError):
            validate_candidate(too_many, allowed_source_ids={"m1"})

    def test_scope_and_duplicate_ids_are_rejected(self) -> None:
        invalid_scope = ConversationMemory(
            constraints=(item("c1", "task scoped", "m1", scope="task"),)
        )
        with self.assertRaises(MemoryValidationError):
            validate_candidate(invalid_scope, allowed_source_ids={"m1"})

        duplicate = ConversationMemory(
            goal=item("same", "目标", "m1"),
            constraints=(item("same", "约束", "m1"),),
        )
        with self.assertRaises(MemoryValidationError):
            validate_candidate(duplicate, allowed_source_ids={"m1"})

    def test_merge_preserves_old_constraints_and_is_idempotent(self) -> None:
        previous = ConversationMemory(
            revision=4,
            generation=2,
            covered_through=2,
            constraints=(item("public-api", "不改公共接口", "m1"),),
        )
        candidate = ConversationMemory(
            revision=4,
            generation=2,
            covered_through=6,
            constraints=(
                item("public-api", "可以修改公共接口", "m5"),
                item("no-vector", "不新增向量库", "m6"),
            ),
        )

        merged = merge_candidate(previous, candidate, allowed_source_ids={"m1", "m5", "m6"})

        self.assertEqual(5, merged.revision)
        self.assertEqual(
            ("不改公共接口", "不新增向量库"),
            tuple(entry.text for entry in merged.constraints),
        )
        self.assertEqual(
            merged,
            merge_candidate(merged, merged, allowed_source_ids={"m1", "m5", "m6"}),
        )

    def test_wrong_generation_or_revision_cannot_commit(self) -> None:
        previous = ConversationMemory(revision=3, generation=7, covered_through=2)
        with self.assertRaises(MemoryValidationError):
            merge_candidate(
                previous,
                ConversationMemory(revision=3, generation=6, covered_through=3),
                allowed_source_ids=set(),
            )
        with self.assertRaises(MemoryValidationError):
            merge_candidate(
                previous,
                ConversationMemory(revision=2, generation=7, covered_through=3),
                allowed_source_ids=set(),
            )

    def test_assigns_stable_sequences_once_and_keeps_tool_pair_separate(self) -> None:
        call = ToolCall("call-1", "read_file", {"path": "README.md"})
        messages = (
            Message("user", "用户任务", kind="task"),
            Message("assistant", None, kind="tool_call", tool_calls=(call,)),
            Message("tool", "内容", kind="tool_result", tool_call_id="call-1"),
            Message("system", "临时装配说明"),
        )

        numbered, next_seq = assign_message_sequences(messages, 1)
        again, next_again = assign_message_sequences(numbered, next_seq)

        self.assertEqual((1, 2, 3, None), tuple(message.message_seq for message in numbered))
        self.assertEqual(("task-1", "task-1", "task-1", None), tuple(message.task_id for message in numbered))
        self.assertEqual(4, next_seq)
        self.assertEqual(numbered, again)
        self.assertEqual(next_seq, next_again)

    def test_old_message_session_context_and_provider_dict_stay_compatible(self) -> None:
        message = Message("user", "hello")
        context = SessionContext(messages=(message,))

        self.assertEqual({"role": "user", "content": "hello"}, message.as_dict())
        self.assertIsNone(message.message_seq)
        self.assertEqual(1, context.next_message_seq)
        self.assertEqual(ConversationMemory(), context.conversation_memory)

    def test_agent_success_preserves_memory_fields_and_numbers_new_history(self) -> None:
        memory = ConversationMemory(
            revision=2,
            generation=4,
            covered_through=7,
            constraints=(item("c1", "保持约束", "m7"),),
        )
        context = SessionContext(
            conversation_memory=memory,
            next_message_seq=8,
            persisted_memory_revision=1,
            memory_pending_clear=True,
        )

        class Provider:
            def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
                return ProviderResponse(
                    tool_calls=(ToolCall("finish-1", "finish", {"summary": "完成"}),),
                    finish_reason="tool_calls",
                )

        class Tools:
            definitions = (ToolDefinition("finish", "结束", {"type": "object"}),)

            @staticmethod
            def contains(name: str) -> bool:
                return name == "finish"

            @staticmethod
            def describe(name: str):  # type: ignore[no-untyped-def]
                return Tools.definitions[0] if name == "finish" else None

            @staticmethod
            def requires_approval(name: str) -> bool:
                return False

            @staticmethod
            def execute(name: str, arguments: dict[str, object], **kwargs):  # type: ignore[no-untyped-def]
                return ToolResult(True, str(arguments.get("summary", "")))

        turn = CodingAgent(Provider(), Tools(), plan_enabled=False).run_with_context(
            "继续任务",
            context,
        )

        self.assertTrue(turn.result.ok)
        self.assertEqual(memory, turn.context.conversation_memory)
        self.assertEqual(1, turn.context.persisted_memory_revision)
        self.assertTrue(turn.context.memory_pending_clear)
        self.assertEqual((8, 9, 10), tuple(message.message_seq for message in turn.context.messages))
        self.assertEqual(("task-8",) * 3, tuple(message.task_id for message in turn.context.messages))
        self.assertEqual(11, turn.context.next_message_seq)


if __name__ == "__main__":
    unittest.main()
