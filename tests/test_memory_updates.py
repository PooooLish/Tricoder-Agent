from __future__ import annotations

import json
import unittest
from dataclasses import replace

from tricoder.context.memory import (
    MEMORY_SCHEMA_VERSION,
    ArchivedMemoryItem,
    ConversationMemory,
    MemoryCapacityError,
    MemoryItem,
    MemoryValidationError,
    conversation_memory_message,
    memory_from_json,
    memory_to_json,
    merge_candidate,
    merge_review_candidate,
)


def _item(
    item_id: str,
    text: str,
    source: str = "m1",
    *,
    state: str = "active",
    replaces_id: str | None = None,
    scope: str = "session",
    task_id: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id,
        text,
        (source,),
        scope,
        task_id,
        state=state,
        replaces_id=replaces_id,
    )


class MemorySchemaCompatibilityTests(unittest.TestCase):
    def test_v1_payload_loads_with_type_specific_defaults_and_saves_as_v2(self) -> None:
        """旧待办映射 pending、旧决策映射 active，重新保存使用 v2。"""

        raw = json.dumps(
            {
                "schema_version": 1,
                "revision": 3,
                "generation": 1,
                "covered_through": 4,
                "goal": {
                    "id": "goal",
                    "text": "旧目标",
                    "source_ids": ["m1"],
                    "scope": "task",
                    "task_id": "task-1",
                },
                "constraints": [],
                "decisions": [
                    {
                        "id": "decision",
                        "text": "旧决定",
                        "source_ids": ["m2"],
                        "scope": "session",
                        "task_id": None,
                    }
                ],
                "open_items": [
                    {
                        "id": "todo",
                        "text": "旧待办",
                        "source_ids": ["m3"],
                        "scope": "session",
                        "task_id": None,
                    }
                ],
            },
            ensure_ascii=False,
        )

        memory = memory_from_json(
            raw,
            allowed_source_ids={"m1", "m2", "m3"},
        )

        self.assertEqual(2, MEMORY_SCHEMA_VERSION)
        self.assertEqual("active", memory.goal.state)
        self.assertEqual("active", memory.decisions[0].state)
        self.assertEqual("pending", memory.open_items[0].state)
        self.assertEqual((), memory.archived)
        self.assertEqual(2, json.loads(memory_to_json(memory))["schema_version"])

    def test_unknown_state_and_version_are_rejected(self) -> None:
        with self.assertRaises(MemoryValidationError):
            memory_to_json(
                ConversationMemory(
                    constraints=(_item("bad", "错误状态", state="verified"),),
                )
            )
        payload = json.loads(memory_to_json(ConversationMemory()))
        payload["schema_version"] = 99
        with self.assertRaises(MemoryValidationError):
            memory_from_json(json.dumps(payload), allowed_source_ids=set())


class MemoryControlledUpdateTests(unittest.TestCase):
    def test_pending_todo_update_replaces_same_id_without_duplication(self) -> None:
        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            open_items=(_item("todo", "旧待办", state="pending"),),
        )
        proposed = ConversationMemory(
            revision=1,
            covered_through=2,
            open_items=(_item("todo", "更新后的待办", "m2", state="pending"),),
        )

        merged = merge_review_candidate(
            previous,
            proposed,
            allowed_source_ids={"m2"},
        )

        self.assertEqual(1, len(merged.open_items))
        self.assertEqual("更新后的待办", merged.open_items[0].text)

    def test_runtime_merge_keeps_constraint_but_review_merge_can_stage_confirmed_change(self) -> None:
        """运行时不接受同 ID 改写；独立待保存候选可展示并等待确认。"""

        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            constraints=(_item("api", "不改公共接口"),),
        )
        proposed = ConversationMemory(
            revision=1,
            covered_through=2,
            constraints=(_item("api", "允许修改公共接口", "m2"),),
        )

        runtime = merge_candidate(
            previous,
            proposed,
            allowed_source_ids={"m2"},
        )
        review = merge_review_candidate(
            previous,
            proposed,
            allowed_source_ids={"m2"},
        )

        self.assertEqual("不改公共接口", runtime.constraints[0].text)
        self.assertEqual("允许修改公共接口", review.constraints[0].text)

    def test_done_todo_leaves_active_prompt_and_moves_to_archive(self) -> None:
        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            open_items=(_item("todo", "补测试", state="pending"),),
        )
        proposed = ConversationMemory(
            revision=1,
            covered_through=2,
            open_items=(_item("todo", "补测试", "m2", state="done"),),
        )

        merged = merge_review_candidate(
            previous,
            proposed,
            allowed_source_ids={"m2"},
        )

        self.assertEqual((), merged.open_items)
        self.assertEqual("open_items", merged.archived[0].section)
        self.assertEqual("done", merged.archived[0].item.state)

    def test_new_goal_and_replacing_decision_archive_previous_items(self) -> None:
        previous = ConversationMemory(
            revision=1,
            covered_through=2,
            goal=_item(
                "goal-a",
                "完成任务 A",
                scope="task",
                task_id="task-a",
            ),
            decisions=(_item("old-decision", "使用旧方案"),),
        )
        proposed = ConversationMemory(
            revision=1,
            covered_through=4,
            goal=_item(
                "goal-b",
                "完成任务 B",
                "m3",
                scope="task",
                task_id="task-b",
            ),
            decisions=(
                _item(
                    "new-decision",
                    "使用新方案",
                    "m4",
                    replaces_id="old-decision",
                ),
            ),
        )

        merged = merge_review_candidate(
            previous,
            proposed,
            allowed_source_ids={"m3", "m4"},
        )

        self.assertEqual("goal-b", merged.goal.id)
        self.assertEqual(("new-decision",), tuple(item.id for item in merged.decisions))
        self.assertEqual(
            {("goal", "goal-a"), ("decisions", "old-decision")},
            {(entry.section, entry.item.id) for entry in merged.archived},
        )

    def test_repeating_the_same_decision_replacement_is_idempotent(self) -> None:
        """old→new 再次出现时只推进覆盖水位，不重复归档或报错。"""

        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            decisions=(_item("old", "旧方案"),),
        )
        replacement = _item(
            "new",
            "新方案",
            "m2",
            replaces_id="old",
        )
        first = merge_review_candidate(
            previous,
            ConversationMemory(
                revision=1,
                covered_through=2,
                decisions=(replacement,),
            ),
            allowed_source_ids={"m2"},
        )

        repeated = merge_review_candidate(
            first,
            ConversationMemory(
                revision=first.revision,
                covered_through=3,
                decisions=(replacement,),
            ),
            allowed_source_ids=set(),
        )

        self.assertEqual((replacement,), repeated.decisions)
        self.assertEqual(first.archived, repeated.archived)
        self.assertEqual(3, repeated.covered_through)

    def test_repeated_replacement_uses_active_metadata_after_archive_cleanup(self) -> None:
        """用户删除旧归档后，既有 active new 的替代元数据仍足以判定重复。"""

        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            decisions=(_item("old", "旧方案"),),
        )
        replacement = _item(
            "new",
            "新方案",
            "m2",
            replaces_id="old",
        )
        first = merge_review_candidate(
            previous,
            ConversationMemory(
                revision=1,
                covered_through=2,
                decisions=(replacement,),
            ),
            allowed_source_ids={"m2"},
        )
        cleaned = replace(first, archived=())

        repeated = merge_review_candidate(
            cleaned,
            ConversationMemory(
                revision=cleaned.revision,
                covered_through=3,
                decisions=(replacement,),
            ),
            allowed_source_ids=set(),
        )

        self.assertEqual((replacement,), repeated.decisions)
        self.assertEqual((), repeated.archived)

    def test_conflicting_replacement_relation_stays_strict(self) -> None:
        """同一 new ID 改称替代另一个未知 ID，不能伪装成幂等重放。"""

        existing = ConversationMemory(
            revision=2,
            covered_through=2,
            decisions=(_item("new", "新方案", "m2", replaces_id="old"),),
        )
        conflicting = ConversationMemory(
            revision=2,
            covered_through=3,
            decisions=(
                _item("new", "新方案", "m3", replaces_id="other"),
            ),
        )

        with self.assertRaises(MemoryValidationError):
            merge_review_candidate(
                existing,
                conflicting,
                allowed_source_ids={"m3"},
            )
        self.assertEqual("old", existing.decisions[0].replaces_id)

    def test_repeating_same_terminal_update_does_not_duplicate_archive(self) -> None:
        """同一待办重复 done 只保留一条归档。"""

        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            open_items=(_item("todo", "补测试", state="pending"),),
        )
        done = _item("todo", "补测试", "m2", state="done")
        first = merge_review_candidate(
            previous,
            ConversationMemory(
                revision=1,
                covered_through=2,
                open_items=(done,),
            ),
            allowed_source_ids={"m2"},
        )

        repeated = merge_review_candidate(
            first,
            ConversationMemory(
                revision=first.revision,
                covered_through=3,
                open_items=(done,),
            ),
            allowed_source_ids=set(),
        )

        self.assertEqual((), repeated.open_items)
        self.assertEqual(1, len(repeated.archived))

    def test_repeating_terminal_update_with_new_source_is_idempotent(self) -> None:
        """新摘要来源不同但终结语义相同，不应制造冲突或重复归档。"""

        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            open_items=(_item("todo", "补测试", state="pending"),),
        )
        first_done = _item("todo", "补测试", "m2", state="done")
        first = merge_review_candidate(
            previous,
            ConversationMemory(
                revision=previous.revision,
                covered_through=2,
                open_items=(first_done,),
            ),
            allowed_source_ids={"m2"},
        )
        repeated_done = _item("todo", "补测试", "m3", state="done")

        repeated = merge_review_candidate(
            first,
            ConversationMemory(
                revision=first.revision,
                covered_through=3,
                open_items=(repeated_done,),
            ),
            allowed_source_ids={"m3"},
        )

        self.assertEqual((), repeated.open_items)
        self.assertEqual((first.archived[0],), repeated.archived)

    def test_capacity_archives_terminal_todo_but_never_drops_active_constraints(self) -> None:
        pending = tuple(
            _item(f"todo-{index}", f"待办 {index}", state="pending")
            for index in range(20)
        )
        previous = ConversationMemory(
            revision=1,
            covered_through=1,
            open_items=pending,
        )
        extra = _item("todo-20", "第 21 个待办", "m2", state="pending")
        with self.assertRaisesRegex(MemoryCapacityError, "归档|选择"):
            merge_review_candidate(
                previous,
                ConversationMemory(
                    revision=1,
                    covered_through=2,
                    open_items=(extra,),
                ),
                allowed_source_ids={"m2"},
            )

        reorganized = merge_review_candidate(
            previous,
            ConversationMemory(
                revision=1,
                covered_through=2,
                open_items=(
                    _item("todo-0", "待办 0", "m2", state="done"),
                    extra,
                ),
            ),
            allowed_source_ids={"m2"},
        )
        self.assertEqual(20, len(reorganized.open_items))
        self.assertIn("todo-20", {item.id for item in reorganized.open_items})
        self.assertEqual("todo-0", reorganized.archived[0].item.id)

        constraints = tuple(
            _item(f"constraint-{index}", f"约束 {index}")
            for index in range(20)
        )
        constrained = ConversationMemory(
            revision=1,
            covered_through=1,
            constraints=constraints,
        )
        with self.assertRaises(MemoryCapacityError):
            merge_review_candidate(
                constrained,
                ConversationMemory(
                    revision=1,
                    covered_through=2,
                    constraints=(_item("constraint-20", "第 21 个约束", "m2"),),
                ),
                allowed_source_ids={"m2"},
            )
        self.assertEqual(20, len(constrained.constraints))

    def test_archived_items_are_persisted_but_not_injected_into_prompt(self) -> None:
        memory = ConversationMemory(
            revision=2,
            covered_through=2,
            constraints=(_item("active", "仍然有效"),),
            archived=(
                ArchivedMemoryItem(
                    "open_items",
                    _item("done", "ARCHIVE-SENTINEL", state="done"),
                ),
            ),
        )

        encoded = memory_to_json(memory)
        prompt = conversation_memory_message(memory)

        self.assertIn("ARCHIVE-SENTINEL", encoded)
        self.assertIsNotNone(prompt)
        self.assertNotIn("ARCHIVE-SENTINEL", prompt.content or "")


if __name__ == "__main__":
    unittest.main()
