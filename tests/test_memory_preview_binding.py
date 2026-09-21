from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tricoder.context.memory import ConversationMemory, MemoryItem
from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, SessionContext
from tricoder.session_runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.sessions import SessionStore


class _NoopAgent:
    """预览绑定测试不进入 Agent 循环。"""


class MemoryPreviewBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.database = (self.root / "state" / "sessions.db").resolve()
        ids = iter(("session-a", "session-b"))
        store = SessionStore(self.database, id_factory=lambda: next(ids))
        store.initialize(self.workspace)
        self.session_a = store.create("a", self.workspace, "openai", "test")
        self.session_b = store.create("b", self.workspace, "openai", "test")
        config = AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                "openai",
                "synthetic-test-key",
                "https://api.openai.com/v1",
                "test",
            ),
            audit_dir=(self.root / "audit").resolve(),
            memory=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
        )

        def factory(record, memory, options):  # type: ignore[no-untyped-def]
            return ActiveSession(
                record,
                memory,
                SessionContext(persisted_summary=memory.summary),
                config,
                _NoopAgent(),
            )

        self.runtime = SessionRuntime(
            SessionStore(self.database),
            self.workspace,
            options=RuntimeOptions(),
            active_session_factory=factory,
        )
        self.runtime.switch(self.session_a.id, confirm=lambda _workspace: True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _memory(text: str, *, generation: int = 2) -> ConversationMemory:
        return ConversationMemory(
            revision=1,
            generation=generation,
            covered_through=2,
            constraints=(MemoryItem("api", text, ("m1",), "session"),),
        )

    def _set_current_memory(self, memory: ConversationMemory) -> None:
        self.runtime.current = replace(
            self.runtime.current,
            context=replace(
                self.runtime.current.context,
                conversation_memory=memory,
                next_message_seq=3,
                latest_completed_task_seq=memory.covered_through,
                persisted_memory_revision=None,
            ),
        )

    def test_edit_preview_rejects_another_session_with_same_counts(self) -> None:
        """缺少 session 绑定时，A 的编辑会错误覆盖计数相同的 B。"""

        self._set_current_memory(self._memory("会话 A 约束"))
        preview = self.runtime.preview_memory_edit("api", "A 的新约束", "session")

        self.runtime.switch(self.session_b.id, confirm=lambda _workspace: True)
        original_b = self._memory("会话 B 约束")
        self._set_current_memory(original_b)

        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.apply_memory_edit(preview)

        self.assertEqual(original_b, self.runtime.current.context.conversation_memory)
        self.assertEqual((), self.runtime.current.context.messages)

    def test_save_preview_rejects_another_session_even_when_candidate_matches(self) -> None:
        """内容完全相同也不能把 A 的保存确认重定位到 B。"""

        shared = self._memory("两个会话碰巧相同")
        self._set_current_memory(shared)
        preview = self.runtime.preview_memory_save()

        self.runtime.switch(self.session_b.id, confirm=lambda _workspace: True)
        self._set_current_memory(shared)

        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.save_memory_preview(preview)

        self.assertIsNone(
            self.runtime.store.load_conversation_memory(self.session_b.id)
        )

    def test_edit_preview_rejects_same_revision_when_original_memory_changed(self) -> None:
        """revision 相同但完整原状态不同仍属于陈旧预览。"""

        self._set_current_memory(self._memory("预览时约束"))
        preview = self.runtime.preview_memory_edit("api", "用户修正", "session")
        changed = self._memory("并发替换后的约束")
        self._set_current_memory(changed)

        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.apply_memory_edit(preview)

        self.assertEqual(changed, self.runtime.current.context.conversation_memory)

    def test_edit_preview_rejects_old_generation_even_if_other_counts_match(self) -> None:
        """clear 代次变化不能靠伪造相同 revision 和序号绕过。"""

        original = self._memory("清除前约束", generation=2)
        self._set_current_memory(original)
        preview = self.runtime.preview_memory_edit("api", "旧代次修改", "session")
        cleared_generation = self._memory("新代次约束", generation=3)
        self._set_current_memory(cleared_generation)

        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.apply_memory_edit(preview)

        self.assertEqual(
            cleared_generation,
            self.runtime.current.context.conversation_memory,
        )

    def test_apply_revalidates_tampered_edit_candidate(self) -> None:
        """提交入口不能信任曾经展示过的 preview 对象仍保持原样。"""

        original = self._memory("原约束")
        self._set_current_memory(original)
        preview = self.runtime.preview_memory_edit("api", "正常修正", "session")
        tampered = replace(
            preview,
            candidate=replace(
                preview.candidate,
                constraints=(
                    MemoryItem("api", "伪造来源", ("m999",), "session"),
                ),
            ),
        )

        with self.assertRaisesRegex(SessionRuntimeError, "候选无效"):
            self.runtime.apply_memory_edit(tampered)

        self.assertEqual(original, self.runtime.current.context.conversation_memory)

    def test_save_rechecks_sensitive_content_at_commit_boundary(self) -> None:
        """预览对象被替换后，最终保存仍需重新执行敏感内容检查。"""

        original = self._memory("可保存约束")
        self._set_current_memory(original)
        preview = self.runtime.preview_memory_save()
        tampered = replace(
            preview,
            candidate=replace(
                original,
                constraints=(
                    MemoryItem("api", "api_key=SYNTHETIC-SENTINEL", ("m1",), "session"),
                ),
            ),
        )

        with self.assertRaisesRegex(SessionRuntimeError, "敏感|重新预览"):
            self.runtime.save_memory_preview(tampered)

        self.assertIsNone(
            self.runtime.store.load_conversation_memory(self.session_a.id)
        )

    def test_save_rejects_candidate_replaced_after_exact_preview(self) -> None:
        """提交只能保存预览时绑定的当前候选，不能换成另一个合法对象。"""

        original = self._memory("原候选")
        self._set_current_memory(original)
        preview = self.runtime.preview_memory_save()
        tampered = replace(
            preview,
            candidate=replace(
                preview.candidate,
                constraints=(
                    MemoryItem("api", "未展示的替换内容", ("m1",), "session"),
                ),
            ),
        )

        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.save_memory_preview(tampered)
        self.assertIsNone(
            self.runtime.store.load_conversation_memory(self.session_a.id)
        )

    def test_valid_edit_applies_once_and_duplicate_confirmation_is_rejected(self) -> None:
        """有效编辑增加 revision；同一确认不能重复应用。"""

        self._set_current_memory(self._memory("原约束"))
        preview = self.runtime.preview_memory_edit("api", "确认后的约束", "session")

        self.runtime.apply_memory_edit(preview)

        self.assertEqual(2, self.runtime.current.context.conversation_memory.revision)
        self.assertEqual(4, self.runtime.current.context.next_message_seq)
        with self.assertRaisesRegex(SessionRuntimeError, "重新预览"):
            self.runtime.apply_memory_edit(preview)
        self.assertEqual(1, len(self.runtime.current.context.messages))

    def test_same_saved_version_is_idempotent_with_a_fresh_preview(self) -> None:
        """相同版本可重新预览后幂等保存，但旧预览仍受绑定约束。"""

        memory = self._memory("幂等保存")
        self._set_current_memory(memory)

        self.runtime.save_memory_preview(self.runtime.preview_memory_save())
        self.runtime.save_memory_preview(self.runtime.preview_memory_save())

        self.assertEqual(
            (memory, 3),
            self.runtime.store.load_conversation_memory(self.session_a.id),
        )


if __name__ == "__main__":
    unittest.main()
