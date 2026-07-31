import inspect
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tricoder.config import ConfigError
from tricoder.models import AppConfig, Message, ProviderConfig, RunResult, SessionContext, SessionTurnResult
from tricoder.providers import create_provider
from tricoder.session_runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.sessions import SessionStore


class FakeAgent:
    """只返回受控结果，避免测试触发真实 Provider。"""

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        self.calls: list[str] = []

    def run_with_context(self, task: str, context: SessionContext) -> SessionTurnResult:
        self.calls.append(task)
        return SessionTurnResult(
            RunResult(
                True,
                "完成安全摘要",
                1,
                modified_files=("src/changed.py",),
                verification="通过",
            ),
            SessionContext(
                messages=context.messages + (Message("user", task, kind="task"),),
                persisted_summary=context.persisted_summary,
                modified_files=("src/changed.py",),
                verification="通过",
            ),
        )


@dataclass
class FakeBuilder:
    """记录候选构建，且可在指定会话上模拟构建失败。"""

    failures: set[str]

    def __post_init__(self) -> None:
        self.built: list[str] = []
        self.records: list[object] = []

    def __call__(self, record, memory, options) -> ActiveSession:  # type: ignore[no-untyped-def]
        self.built.append(record.id)
        self.records.append(record)
        if record.id in self.failures or record.name in self.failures:
            raise ValueError("模拟候选构建失败")
        config = AppConfig(
            workspace=record.workspace,
            provider=ProviderConfig(record.provider, "test-key", "https://example.test", record.model),
        )
        return ActiveSession(record, memory, SessionContext(persisted_summary=memory.summary), config, FakeAgent(record.provider))


class FailingMemoryStore:
    """仅让运行期保存失败，模拟 SQLite 临时不可写。"""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.fail_writes = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.store, name)

    def save_memory(self, session_id: str, memory) -> None:  # type: ignore[no-untyped-def]
        if self.fail_writes:
            raise OSError("模拟 SQLite 写入失败")
        self.store.save_memory(session_id, memory)


class SessionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = (self.root / "one").resolve()
        self.other_workspace = (self.root / "two").resolve()
        self.workspace.mkdir()
        self.other_workspace.mkdir()
        self.store = SessionStore((self.root / "state" / "sessions.db").resolve())
        self.store.initialize(self.workspace)
        self.first = self.store.create("first", self.workspace, "openai", "model-a")
        self.second = self.store.create("second", self.other_workspace, "glm", "model-b")
        self.builder = FakeBuilder(set())
        self.runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=self.builder,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_runtime_uses_shared_provider_factory_by_default(self) -> None:
        """防止交互会话与 CLI 使用不同的厂商注册表。"""
        default_factory = inspect.signature(SessionRuntime).parameters[
            "provider_factory"
        ].default

        self.assertIs(create_provider, default_factory)

    def test_startup_restores_only_latest_session_for_current_workspace(self) -> None:
        """防止启动时错误跳转到另一个工作区的最新会话。"""
        self.assertEqual(self.first.id, self.runtime.current.record.id)
        self.assertEqual([self.first.id], self.builder.built)

    def test_preview_models_uses_injected_resolver_without_provider_build(self) -> None:
        """模型预览只解析名称，不触发 Key 校验、Provider 或 Agent 构建。"""
        calls: list[tuple[Path, object, object]] = []

        def resolver(workspace, *, environ, model):  # type: ignore[no-untyped-def]
            calls.append((workspace, environ, model))
            return {"openai": "preview-openai", "deepseek": "preview-deepseek", "glm": "preview-glm"}

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
            model_preview_resolver=resolver,
        )

        self.assertEqual("preview-glm", runtime.preview_models()["glm"])
        self.assertEqual([(self.workspace, {}, None)], calls)

    def test_startup_creates_default_when_workspace_has_no_session(self) -> None:
        """防止无历史会话的工作区无法获得独立默认会话。"""
        empty_workspace = (self.root / "empty").resolve()
        empty_workspace.mkdir()
        runtime = SessionRuntime(
            self.store,
            empty_workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        self.assertEqual("default", runtime.current.record.name)
        self.assertEqual(empty_workspace, runtime.current.record.workspace)

    def test_default_startup_failure_does_not_create_orphan_session(self) -> None:
        """防止首次启动的候选构建失败后在 SQLite 留下 default 孤儿会话。"""
        empty_workspace = (self.root / "empty-failed").resolve()
        empty_workspace.mkdir()
        builder = FakeBuilder({"default"})

        with self.assertRaises(SessionRuntimeError):
            SessionRuntime(
                self.store,
                empty_workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=builder,
            )

        self.assertFalse(
            any(item.workspace == empty_workspace for item in self.store.list_all())
        )

    def test_startup_explicit_provider_replaces_restored_model_after_validation(self) -> None:
        """防止显式启动 Provider 被旧会话静默覆盖，或未写回验证后的模型。"""
        def config_loader(**kwargs):  # type: ignore[no-untyped-def]
            provider = kwargs["provider"]
            return AppConfig(
                workspace=self.workspace,
                provider=ProviderConfig(provider, "test-key", "https://example.test", f"{provider}-model"),
                audit_dir=self.root / "audit",
            )

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}, provider="glm"),
            config_loader=config_loader,
            provider_factory=lambda _config, _timeout: object(),
            agent_factory=lambda *_args, **_kwargs: FakeAgent("glm"),
        )

        self.assertEqual("glm", runtime.current.record.provider)
        self.assertEqual("glm-model", runtime.current.record.model)
        self.assertEqual("glm", self.store.get(self.first.id).provider)

    def test_cross_workspace_switch_rebuilds_before_commit(self) -> None:
        """防止跨工作区切换在候选配置完成前替换当前会话。"""
        switched = self.runtime.switch(self.second.id, confirm=lambda _: True)

        self.assertEqual(self.second.id, switched.record.id)
        self.assertEqual(self.other_workspace, self.runtime.current.config.workspace)
        self.assertEqual([self.first.id, self.second.id], self.builder.built)

    def test_create_failure_does_not_create_orphan_session(self) -> None:
        """防止新会话候选构建失败后仍写入不可激活的 Session 记录。"""
        self.builder.failures.add("new")
        before = self.store.list_all()

        with self.assertRaises(SessionRuntimeError):
            self.runtime.create("new")

        self.assertEqual(before, self.store.list_all())

    def test_new_session_candidate_uses_its_final_unique_record_id(self) -> None:
        """防止候选 Agent 和审计器使用共享临时 ID，而非最终持久化 ID。"""
        first_new = self.runtime.create("new-one")
        second_new = self.runtime.create("new-two")
        candidate_records = [
            record for record in self.builder.records if record.name in {"new-one", "new-two"}
        ]

        self.assertEqual(first_new.record.id, candidate_records[0].id)
        self.assertEqual(second_new.record.id, candidate_records[1].id)
        self.assertNotEqual(first_new.record.id, second_new.record.id)

    def test_default_candidate_uses_the_final_persisted_record_id(self) -> None:
        """防止首次默认会话在候选审计路径中使用固定共享 ID。"""
        empty_workspace = (self.root / "empty-real-id").resolve()
        empty_workspace.mkdir()
        builder = FakeBuilder(set())

        runtime = SessionRuntime(
            self.store,
            empty_workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=builder,
        )

        self.assertEqual(runtime.current.record.id, builder.records[-1].id)

    def test_invalid_session_name_does_not_build_a_candidate(self) -> None:
        """防止无效名称在校验前触发 Agent、审计器或其他候选副作用。"""
        before = list(self.builder.built)

        with self.assertRaises(SessionRuntimeError):
            self.runtime.create("bad\nname")

        self.assertEqual(before, self.builder.built)

    def test_failed_rebuild_keeps_original_session(self) -> None:
        """防止候选 Agent 构建失败留下半切换的运行时状态。"""
        original = self.runtime.current
        self.builder.failures.add(self.second.id)

        with self.assertRaises(SessionRuntimeError):
            self.runtime.switch(self.second.id, confirm=lambda _: True)

        self.assertIs(original, self.runtime.current)

    def test_clean_switch_failure_does_not_write_current_session(self) -> None:
        """防止干净会话因目标候选失败而仅更新时间或重复写入记忆。"""
        before_record = self.store.get(self.first.id)
        before_memory = self.store.load_memory(self.first.id)
        self.builder.failures.add(self.second.id)

        with self.assertRaises(SessionRuntimeError):
            self.runtime.switch(self.second.id, confirm=lambda _: True)

        self.assertEqual(before_record, self.store.get(self.first.id))
        self.assertEqual(before_memory, self.store.load_memory(self.first.id))

    def test_cross_workspace_rejection_keeps_original_session(self) -> None:
        """防止用户拒绝跨工作区确认后仍然更换会话。"""
        original = self.runtime.current

        returned = self.runtime.switch(self.second.id, confirm=lambda _: False)

        self.assertIs(original, returned)
        self.assertIs(original, self.runtime.current)
        self.assertEqual([self.first.id], self.builder.built)

    def test_sessions_do_not_share_context_provider_or_workspace(self) -> None:
        """防止切换会话后复用前一会话的上下文、Provider 或工作区。"""
        self.runtime.run_task("first task")
        first_context = self.runtime.current.context
        self.runtime.switch(self.second.id, confirm=lambda _: True)
        self.runtime.run_task("second task")

        self.assertNotEqual(first_context, self.runtime.current.context)
        self.assertEqual("glm", self.runtime.current.config.provider.name)
        self.assertEqual(self.other_workspace, self.runtime.current.config.workspace)

    def test_switching_back_restores_each_open_session_complete_context(self) -> None:
        """防止 A→B→A 时从 SQLite 摘要重建并丢失进程内完整消息。"""
        self.runtime.run_task("first task")
        first_active = self.runtime.current
        first_context = first_active.context

        self.runtime.switch(self.second.id, confirm=lambda _: True)
        self.runtime.run_task("second task")
        second_active = self.runtime.current
        second_context = second_active.context

        restored_first = self.runtime.switch(self.first.id, confirm=lambda _: True)
        restored_second = self.runtime.switch(self.second.id, confirm=lambda _: True)

        self.assertEqual(first_context, restored_first.context)
        self.assertEqual(second_context, restored_second.context)
        self.assertIs(first_active.agent, restored_first.agent)
        self.assertIs(second_active.agent, restored_second.agent)
        self.assertIsNot(restored_first.agent, restored_second.agent)
        self.assertEqual(["first task"], [message.content for message in restored_first.context.messages])
        self.assertEqual(["second task"], [message.content for message in restored_second.context.messages])
        self.assertEqual([self.first.id, self.second.id], self.builder.built)

    def test_model_change_preserves_complete_context_and_memory(self) -> None:
        """防止 /model 成功重建 Agent 时清空当前消息或替换安全记忆。"""
        def config_loader(**kwargs):  # type: ignore[no-untyped-def]
            provider = kwargs["provider"]
            return AppConfig(
                workspace=self.workspace,
                provider=ProviderConfig(
                    provider,
                    "test-key",
                    "https://example.test",
                    f"{provider}-model",
                ),
            )

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
            config_loader=config_loader,
        )
        runtime.run_task("keep this message")
        original = runtime.current

        changed = runtime.change_model("glm")

        self.assertEqual(original.context, changed.context)
        self.assertEqual(original.memory, changed.memory)
        self.assertEqual(
            ["keep this message"],
            [message.content for message in changed.context.messages],
        )
        self.assertIsNot(original.agent, changed.agent)
        self.assertEqual("glm", changed.record.provider)

    def test_persist_failure_keeps_memory_and_marks_runtime_unsaved(self) -> None:
        """防止 SQLite 临时失败时丢失内存记忆或伪装为已保存。"""
        wrapped_store = FailingMemoryStore(self.store)
        runtime = SessionRuntime(
            wrapped_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        wrapped_store.fail_writes = True

        result = runtime.run_task("检查模块")

        self.assertTrue(result.ok)
        self.assertEqual(
            "run: succeeded; modified_files=1; verification=passed",
            runtime.current.memory.summary,
        )
        self.assertTrue(runtime.status().unsaved_memory)
        self.assertIn("未持久化", runtime.status().warning)
        self.assertFalse(runtime.persist_current())

    def test_switch_cancels_when_current_memory_cannot_be_persisted(self) -> None:
        """防止当前安全摘要未保存时仍切换，导致重启后丢失记忆。"""
        wrapped_store = FailingMemoryStore(self.store)
        builder = FakeBuilder(set())
        runtime = SessionRuntime(
            wrapped_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=builder,
        )
        wrapped_store.fail_writes = True
        runtime.run_task("检查模块")
        original = runtime.current

        with self.assertRaises(SessionRuntimeError):
            runtime.switch(self.second.id, confirm=lambda _: True)

        self.assertIs(original, runtime.current)

    def test_create_dirty_session_is_atomic_when_current_persist_fails(self) -> None:
        """防止 /session new 在旧 dirty 记忆保存失败后插入或切换新会话。"""
        wrapped_store = FailingMemoryStore(self.store)
        runtime = SessionRuntime(
            wrapped_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        wrapped_store.fail_writes = True
        runtime.run_task("需要稍后重试")
        original = runtime.current
        original_warning = runtime.status().warning
        original_ids = [record.id for record in self.store.list_all()]

        with self.assertRaises(SessionRuntimeError):
            runtime.create("must-not-exist")

        self.assertIs(original, runtime.current)
        self.assertEqual(original_ids, [record.id for record in self.store.list_all()])
        self.assertNotIn("must-not-exist", [record.name for record in self.store.list_all()])
        self.assertTrue(runtime.status().unsaved_memory)
        self.assertEqual(original_warning, runtime.status().warning)

        wrapped_store.fail_writes = False
        self.assertTrue(runtime.retry_persist())
        self.assertFalse(runtime.status().unsaved_memory)
        self.assertEqual(original.memory, self.store.load_memory(original.record.id))

    def test_clear_preserves_file_metadata_and_retries_persistence(self) -> None:
        """防止清空摘要时删除结构化文件元数据，或使退出重试无入口。"""
        self.runtime.run_task("修改模块")
        original_files = self.runtime.current.memory.modified_files

        self.runtime.clear_current()

        self.assertEqual((), self.runtime.current.context.messages)
        self.assertEqual("", self.runtime.current.memory.summary)
        self.assertEqual(original_files, self.runtime.current.memory.modified_files)
        self.assertTrue(self.runtime.retry_persist())

    def test_failed_clear_keeps_clear_intent_for_retry(self) -> None:
        """防止清空写入失败后重试错误保存清空前摘要或丢失文件元数据。"""
        wrapped_store = FailingMemoryStore(self.store)
        runtime = SessionRuntime(
            wrapped_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        runtime.run_task("修改模块")
        files = runtime.current.memory.modified_files
        verification = runtime.current.memory.verification
        wrapped_store.fail_writes = True

        with self.assertRaises(SessionRuntimeError):
            runtime.clear_current()

        self.assertEqual("", runtime.current.memory.summary)
        self.assertEqual(files, runtime.current.memory.modified_files)
        self.assertEqual(verification, runtime.current.memory.verification)
        self.assertTrue(runtime.status().unsaved_memory)
        wrapped_store.fail_writes = False

        self.assertTrue(runtime.retry_persist())
        persisted = self.store.load_memory(self.first.id)
        self.assertEqual("", persisted.summary)
        self.assertEqual(files, persisted.modified_files)

    def test_model_failure_keeps_original_session_and_metadata(self) -> None:
        """防止缺失 Provider 配置时提前写入会话模型或替换当前 Agent。"""
        def missing_config(**_kwargs):  # type: ignore[no-untyped-def]
            raise ConfigError("模拟缺失 API Key")

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
            config_loader=missing_config,
        )
        original = runtime.current

        with self.assertRaises(SessionRuntimeError):
            runtime.change_model("glm")

        self.assertIs(original, runtime.current)
        self.assertEqual("openai", self.store.get(self.first.id).provider)

    def test_runtime_persists_safe_memory_not_task_or_message_history(self) -> None:
        """防止用户粘贴的多行源码或 Agent 上下文原文进入 SQLite。"""
        raw_task = "修复问题\nprint('private source')\nAPI_KEY=secret-value"

        self.runtime.run_task(raw_task)
        persisted = self.store.load_memory(self.first.id)

        self.assertIn("原文未持久化", persisted.requirements_summary)
        self.assertNotIn("private source", persisted.requirements_summary)
        self.assertNotIn("secret-value", persisted.requirements_summary)
        self.assertEqual(
            "run: succeeded; modified_files=1; verification=passed",
            persisted.summary,
        )
        self.assertEqual("passed", persisted.verification)


if __name__ == "__main__":
    unittest.main()
