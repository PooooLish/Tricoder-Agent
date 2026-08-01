import inspect
import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from tricoder.audit import AuditLogger
from tricoder.changes import ChangeJournal, FileIdentity, FileSnapshot
from tricoder.config import ConfigError
from tricoder.models import AppConfig, Message, ProviderConfig, RunResult, SessionContext, SessionMemory, SessionTurnResult
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import create_provider
from tricoder.session_runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.sessions import SessionStore
from tricoder.tools import ToolContext, ToolRegistry


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


class CountingJournal(ChangeJournal):
    """记录事务边界，确保 Runtime 每次任务只开闭一次账本。"""

    def __init__(self) -> None:
        super().__init__()
        self.begun = 0
        self.sealed = 0

    def begin_task(self, modified_files: tuple[str, ...], verification: str) -> None:
        self.begun += 1
        super().begin_task(modified_files, verification)

    def seal_task(self, modified_files: tuple[str, ...], verification: str):  # type: ignore[no-untyped-def]
        self.sealed += 1
        return super().seal_task(modified_files, verification)


class SealFailingJournal(CountingJournal):
    """先真实封存，再模拟事务收尾钩子异常。"""

    def seal_task(self, modified_files: tuple[str, ...], verification: str):  # type: ignore[no-untyped-def]
        super().seal_task(modified_files, verification)
        raise RuntimeError("SEAL-ERROR-SENTINEL")


class JournalWritingAgent:
    """在返回前模拟工具已提交写入；不写磁盘，只验证 Runtime 事务编排。"""

    def __init__(self, journal: ChangeJournal, label: str) -> None:
        self.journal = journal
        self.label = label

    def run_with_context(self, task: str, context: SessionContext) -> SessionTurnResult:
        modified_files: tuple[str, ...] = ()
        if task != "no-write":
            path = f"src/{self.label}.py"
            before = FileSnapshot(path, f"{self.label}-before\n", 0o644, FileIdentity(1, 1))
            after = FileSnapshot(path, f"{self.label}-after\n", 0o644, FileIdentity(1, 2))
            self.journal.record_committed(path, before, after)
            modified_files = (path,)
        result = RunResult(
            ok=task != "failed-write",
            summary="受控结果",
            rounds=1,
            modified_files=modified_files,
            verification="failed" if task == "failed-write" else "passed",
        )
        return SessionTurnResult(
            result,
            SessionContext(
                messages=context.messages,
                persisted_summary=context.persisted_summary,
                modified_files=modified_files,
                verification=result.verification,
            ),
        )


class RegistryWritingAgent:
    """通过真实 ToolRegistry 提交一改一建，覆盖 Runtime 与工具账本的装配边界。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def run_with_context(self, _task: str, context: SessionContext) -> SessionTurnResult:
        edited = self.registry.execute(
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": "BEFORE_SENTINEL",
                "new_text": "AFTER_SENTINEL",
            },
        )
        created = self.registry.execute(
            "create_file",
            {"path": "src/created.py", "content": "CREATED_SENTINEL\n"},
        )
        if not edited.ok or not created.ok:
            raise AssertionError((edited, created))
        paths = ("src/app.py", "src/created.py")
        return SessionTurnResult(
            RunResult(True, "完成", 1, modified_files=paths, verification="passed"),
            replace(context, modified_files=paths, verification="passed"),
        )


class VersionWritingAgent:
    """从任务参数读取明确版本，经真实 ToolRegistry 提交一次编辑。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def run_with_context(self, task: str, context: SessionContext) -> SessionTurnResult:
        old_text, new_text = task.split("->", 1)
        result = self.registry.execute(
            "edit_file",
            {
                "path": "src/model.py",
                "old_text": f"{old_text}\n",
                "new_text": f"{new_text}\n",
            },
        )
        if not result.ok:
            raise AssertionError(result)
        paths = ("src/model.py",)
        return SessionTurnResult(
            RunResult(True, "完成", 1, modified_files=paths, verification="passed"),
            replace(context, modified_files=paths, verification="passed"),
        )


class ExplodingJournalAgent:
    """在真实提交账本记录后抛出同一个异常实例。"""

    def __init__(self, journal: ChangeJournal, error: Exception) -> None:
        self.journal = journal
        self.error = error

    def run_with_context(self, _task: str, _context: SessionContext) -> SessionTurnResult:
        before = FileSnapshot("src/error.py", "before\n", 0o644, FileIdentity(1, 1))
        after = FileSnapshot("src/error.py", "after\n", 0o644, FileIdentity(1, 2))
        self.journal.record_committed("src/error.py", before, after)
        raise self.error
@dataclass
class JournalSessionFactory:
    journals: dict[str, CountingJournal]

    def __call__(self, record, memory, _options) -> ActiveSession:  # type: ignore[no-untyped-def]
        journal = CountingJournal()
        self.journals[record.id] = journal
        config = AppConfig(
            workspace=record.workspace,
            provider=ProviderConfig(record.provider, "test-key", "https://example.test", record.model),
        )
        return ActiveSession(
            record,
            memory,
            SessionContext(
                persisted_summary=memory.summary,
                modified_files=memory.modified_files,
                verification=memory.verification,
            ),
            config,
            JournalWritingAgent(journal, record.name),
            journal=journal,
        )


class RegistryJournalFactory:
    """保持旧三参数签名，并让每次候选构建先产生自己的新账本。"""

    def __init__(self) -> None:
        self.journals: list[ChangeJournal] = []

    def __call__(self, record, memory, _options) -> ActiveSession:  # type: ignore[no-untyped-def]
        journal = ChangeJournal()
        self.journals.append(journal)
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(record.workspace),
                CommandPolicy(),
                lambda _action, _detail: True,
                change_journal=journal,
            )
        )
        config = AppConfig(
            workspace=record.workspace,
            provider=ProviderConfig(
                record.provider,
                "test-key",
                "https://example.test",
                record.model,
            ),
        )
        return ActiveSession(
            record,
            memory,
            SessionContext(
                persisted_summary=memory.summary,
                modified_files=memory.modified_files,
                verification=memory.verification,
            ),
            config,
            VersionWritingAgent(registry),
            registry,
            journal,
        )


class LegacyRegistryJournalFactory(RegistryJournalFactory):
    """模拟旧代码只传 ActiveSession 的前五个位置参数。"""

    def __call__(self, record, memory, options) -> ActiveSession:  # type: ignore[no-untyped-def]
        candidate = super().__call__(record, memory, options)
        return ActiveSession(
            candidate.record,
            candidate.memory,
            candidate.context,
            candidate.config,
            candidate.agent,
        )


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

    def _runtime_with_real_registry(
        self,
        store: SessionStore | FailingMemoryStore | None = None,
    ) -> tuple[SessionRuntime, Path]:
        active_store = store or self.store
        memory = SessionMemory(
            modified_files=("src/prior.py",),
            verification="not-run",
        )
        active_store.save_memory(self.first.id, memory)
        runtime = SessionRuntime(
            active_store,  # type: ignore[arg-type]
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        source_dir = self.workspace / "src"
        source_dir.mkdir(exist_ok=True)
        target = source_dir / "app.py"
        target.write_text("value = 'BEFORE_SENTINEL'\n", encoding="utf-8")
        journal = ChangeJournal()
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                lambda _action, _detail: True,
                change_journal=journal,
            )
        )
        audit_path = self.root / "audit" / f"session-{runtime.current.record.id}.jsonl"
        audit = AuditLogger(audit_path)
        audit.prepare()
        runtime.current = ActiveSession(
            runtime.current.record,
            memory,
            SessionContext(
                modified_files=memory.modified_files,
                verification=memory.verification,
            ),
            runtime.current.config,
            RegistryWritingAgent(registry),
            registry,
            journal,
            audit,
        )
        runtime._persisted_memory = memory
        runtime._memory_dirty = False
        runtime._cache_current()
        return runtime, audit_path

    def test_runtime_uses_shared_provider_factory_by_default(self) -> None:
        """防止交互会话与 CLI 使用不同的厂商注册表。"""
        default_factory = inspect.signature(SessionRuntime).parameters[
            "provider_factory"
        ].default

        self.assertIs(create_provider, default_factory)

    def test_failed_write_is_sealed_once_and_no_write_keeps_latest_diff(self) -> None:
        journals: dict[str, CountingJournal] = {}
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=JournalSessionFactory(journals),
        )

        result = runtime.run_task("failed-write")
        first_diff = runtime.diff_latest()
        runtime.run_task("no-write")

        self.assertFalse(result.ok)
        self.assertIsNotNone(first_diff)
        self.assertIn("-first-before", first_diff or "")
        self.assertIn("+first-after", first_diff or "")
        self.assertEqual(runtime.diff_latest(), first_diff)
        self.assertEqual(journals[runtime.current.record.id].begun, 2)
        self.assertEqual(journals[runtime.current.record.id].sealed, 2)

    def test_session_journals_are_isolated_and_survive_clear_model_change(self) -> None:
        def config_loader(**kwargs):  # type: ignore[no-untyped-def]
            provider = kwargs["provider"]
            return AppConfig(
                workspace=self.workspace,
                provider=ProviderConfig(provider, "test-key", "https://example.test", f"{provider}-model"),
            )

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=JournalSessionFactory({}),
            config_loader=config_loader,
        )
        session_a = runtime.current.record.id
        runtime.run_task("write-a")
        diff_a = runtime.diff_latest()

        runtime.create("third")
        session_b = runtime.current.record.id
        runtime.run_task("write-b")
        diff_b = runtime.diff_latest()
        runtime.switch(session_a, confirm=lambda _workspace: True)

        self.assertEqual(runtime.diff_latest(), diff_a)
        runtime.clear_current()
        self.assertEqual(runtime.diff_latest(), diff_a)
        runtime.change_model("glm")
        self.assertEqual(runtime.diff_latest(), diff_a)
        runtime.switch(session_b, confirm=lambda _workspace: True)
        self.assertEqual(runtime.diff_latest(), diff_b)

    def test_three_argument_factory_model_change_rebinds_tools_to_existing_journal(self) -> None:
        source_dir = self.workspace / "src"
        source_dir.mkdir()
        target = source_dir / "model.py"
        target.write_text("v0\n", encoding="utf-8")
        factory = RegistryJournalFactory()

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
            active_session_factory=factory,
            config_loader=config_loader,
        )
        original_journal = runtime.current.journal
        runtime.run_task("v0->v1")
        runtime.change_model("glm")

        runtime.run_task("v1->v2")
        second_diff = runtime.diff_latest()
        execution = runtime.undo_latest()

        self.assertIs(original_journal, runtime.current.journal)
        self.assertIs(
            original_journal,
            runtime.current.tools.context.change_journal,  # type: ignore[union-attr]
        )
        self.assertIn("-v1", second_diff or "")
        self.assertIn("+v2", second_diff or "")
        self.assertNotIn("-v0", second_diff or "")
        self.assertTrue(execution.ok)
        self.assertEqual("v1\n", target.read_text(encoding="utf-8"))

    def test_legacy_five_position_factory_rebinds_agent_registry_across_model_change(self) -> None:
        source_dir = self.workspace / "src"
        source_dir.mkdir()
        target = source_dir / "model.py"
        target.write_text("v0\n", encoding="utf-8")
        factory = LegacyRegistryJournalFactory()

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
            active_session_factory=factory,
            config_loader=config_loader,
        )
        original_journal = runtime.current.journal
        runtime.run_task("v0->v1")
        runtime.change_model("glm")

        runtime.run_task("v1->v2")
        second_diff = runtime.diff_latest()
        execution = runtime.undo_latest()

        self.assertIsNotNone(runtime.current.tools)
        self.assertIs(original_journal, runtime.current.journal)
        self.assertIs(
            original_journal,
            runtime.current.tools.context.change_journal,  # type: ignore[union-attr]
        )
        self.assertIn("-v1", second_diff or "")
        self.assertIn("+v2", second_diff or "")
        self.assertNotIn("-v0", second_diff or "")
        self.assertTrue(execution.ok)
        self.assertEqual("v1\n", target.read_text(encoding="utf-8"))

    def test_restart_does_not_restore_source_snapshots(self) -> None:
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=JournalSessionFactory({}),
        )
        runtime.run_task("write")
        self.assertIsNotNone(runtime.diff_latest())

        restarted = SessionRuntime(
            SessionStore(self.store.database_path),
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=JournalSessionFactory({}),
        )

        self.assertIsNone(restarted.diff_latest())

    def test_agent_exception_after_write_is_sealed_and_reraised_unchanged(self) -> None:
        journals: dict[str, CountingJournal] = {}
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=JournalSessionFactory(journals),
        )
        error = LookupError("AGENT-ERROR-SENTINEL")
        journal = runtime.current.journal
        runtime.current = replace(
            runtime.current,
            agent=ExplodingJournalAgent(journal, error),
        )

        with self.assertRaises(LookupError) as raised:
            runtime.run_task("explode")

        self.assertIs(error, raised.exception)
        self.assertIn("-before", runtime.diff_latest() or "")
        self.assertIn("+after", runtime.diff_latest() or "")
        self.assertEqual(1, journals[runtime.current.record.id].begun)
        self.assertEqual(1, journals[runtime.current.record.id].sealed)

    def test_agent_exception_is_not_masked_by_secondary_seal_failure(self) -> None:
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        journal = SealFailingJournal()
        error = LookupError("PRIMARY-AGENT-ERROR")
        runtime.current = replace(
            runtime.current,
            journal=journal,
            agent=ExplodingJournalAgent(journal, error),
        )

        with self.assertRaises(LookupError) as raised:
            runtime.run_task("explode")

        self.assertIs(error, raised.exception)
        self.assertIsNotNone(journal.latest())

    def test_prepare_and_undo_restore_structured_state_clear_journal_and_audit_safely(self) -> None:
        runtime, audit_path = self._runtime_with_real_registry()
        runtime.run_task("write mixed change")

        preview = runtime.prepare_undo()
        execution = runtime.undo_latest()

        self.assertIn("-value = 'AFTER_SENTINEL'", preview.diff)
        self.assertIn("+value = 'BEFORE_SENTINEL'", preview.diff)
        self.assertTrue(execution.ok)
        self.assertEqual("value = 'BEFORE_SENTINEL'\n", (self.workspace / "src" / "app.py").read_text(encoding="utf-8"))
        self.assertFalse((self.workspace / "src" / "created.py").exists())
        self.assertIsNone(runtime.diff_latest())
        self.assertEqual(("src/prior.py",), runtime.current.context.modified_files)
        self.assertEqual("not-run", runtime.current.context.verification)
        self.assertEqual(("src/prior.py",), runtime.current.memory.modified_files)
        self.assertEqual("not-run", runtime.current.memory.verification)
        persisted = self.store.load_memory(runtime.current.record.id)
        self.assertEqual(("src/prior.py",), persisted.modified_files)
        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("BEFORE_SENTINEL", audit_text)
        self.assertNotIn("AFTER_SENTINEL", audit_text)
        self.assertNotIn(preview.diff, audit_text)
        event = json.loads(audit_text.splitlines()[-1])
        self.assertEqual("undo", event["event"])
        self.assertEqual("succeeded", event["status"])
        self.assertEqual(["src/app.py", "src/created.py"], event["paths"])
        self.assertEqual(
            {
                "timestamp",
                "event",
                "status",
                "paths",
                "file_count",
                "conflict_count",
                "compensation_status",
            },
            set(event),
        )

    def test_successful_undo_keeps_files_restored_when_memory_persist_fails_then_retries(self) -> None:
        wrapped = FailingMemoryStore(self.store)
        runtime, _audit_path = self._runtime_with_real_registry(wrapped)
        runtime.run_task("write mixed change")
        wrapped.fail_writes = True

        execution = runtime.undo_latest()

        self.assertTrue(execution.ok)
        self.assertEqual("value = 'BEFORE_SENTINEL'\n", (self.workspace / "src" / "app.py").read_text(encoding="utf-8"))
        self.assertIsNone(runtime.diff_latest())
        self.assertEqual("本次记忆未持久化", runtime.status().warning)
        wrapped.fail_writes = False
        self.assertTrue(runtime.retry_persist())
        self.assertEqual("", runtime.status().warning)

    def test_read_only_rejects_undo_before_tool_preview_but_keeps_diff_available(self) -> None:
        runtime, _audit_path = self._runtime_with_real_registry()
        runtime.run_task("write mixed change")
        runtime.current = replace(
            runtime.current,
            config=replace(runtime.current.config, read_only=True),
        )
        diff = runtime.diff_latest()

        with self.assertRaises(SessionRuntimeError):
            runtime.prepare_undo()

        self.assertEqual(diff, runtime.diff_latest())
        self.assertEqual("value = 'AFTER_SENTINEL'\n", (self.workspace / "src" / "app.py").read_text(encoding="utf-8"))

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

    def test_rebuild_and_model_change_pass_tool_protocol_to_agent_factory(self) -> None:
        """防止重建或 /model 切换时遗漏已加载的工具协议。"""
        received_protocols: list[str] = []

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
                audit_dir=self.root / "audit",
                tool_protocol="legacy_json",
            )

        def agent_factory(*_args, **kwargs):  # type: ignore[no-untyped-def]
            received_protocols.append(kwargs["tool_protocol"])
            return FakeAgent("configured")

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=config_loader,
            provider_factory=lambda _config, _timeout: object(),
            agent_factory=agent_factory,
        )

        runtime.change_model("glm")

        self.assertEqual(["legacy_json", "legacy_json"], received_protocols)

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
