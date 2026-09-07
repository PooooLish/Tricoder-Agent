import asyncio
import inspect
import json
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

from tricoder.audit import AuditLogger
from tricoder.changes import (
    ChangeJournal,
    FileIdentity,
    FileSnapshot,
    UndoExecution,
)
from tricoder.config import ConfigError
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.context.spill import ToolResultSpillStore
from tricoder.extensions import ToolOrigin
from tricoder.models import (
    AppConfig,
    ExtensionsConfig,
    MCPConfig,
    MCPServerConfig,
    Message,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionMemory,
    SessionTurnResult,
    ToolResult,
)
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
from tricoder.tools.handlers import ToolHandler


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


class BlockingCancellableAgent:
    """等待 Runtime 传入的取消令牌，验证跨线程取消通路。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def run_with_context(
        self,
        task: str,
        context: SessionContext,
        *,
        cancellation=None,  # type: ignore[no-untyped-def]
    ) -> SessionTurnResult:
        if cancellation is None:
            raise AssertionError("Runtime 未传递取消令牌")
        self.started.set()
        if not cancellation.wait(timeout=2):
            raise TimeoutError("测试未收到取消信号")
        self.cancelled.set()
        return SessionTurnResult(RunResult(False, "任务已取消", 0), context)


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


class _MCPProbeHandler(ToolHandler):
    """用于确认临时注册表确实包含 MCP 工具。"""

    name = "mcp__docs__echo"
    description = "fake MCP echo"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, _arguments):  # type: ignore[no-untyped-def]
        return ToolResult(False, "仅测试异步边界")


class _TaskManager:
    """每个任务一实例的 fake manager；不创建 SDK、网络或进程。"""

    def __init__(self, owner, config, context, source_env, audit):  # type: ignore[no-untyped-def]
        self.owner = owner
        self.config = config
        self.context = context
        self.source_env = source_env
        self.audit = audit
        self.stopped = False

    async def start_all(self, _cancellation):  # type: ignore[no-untyped-def]
        self.owner.manager_events.append(("start", self))

    def register_tools(self, registry: ToolRegistry) -> int:
        registry.register(
            _MCPProbeHandler(registry.context),
            origin=ToolOrigin("mcp", "docs", "dangerous"),
        )
        self.owner.manager_events.append(("register", self))
        return 1

    def unregister_tools(self, registry: ToolRegistry) -> int:
        handler = registry._handlers.get("mcp__docs__echo")
        return int(handler is not None and registry.unregister(handler))

    async def stop_all(self) -> None:
        self.stopped = True
        self.owner.manager_events.append(("stop", self))


class MCPRuntimeFactory:
    """为 SessionRuntime 构造启用 MCP 的长期会话与任务级 Agent。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manager_events: list[tuple[str, _TaskManager]] = []
        self.task_agents: list[object] = []
        self.audit = mock.sentinel.mcp_audit
        self.spill = mock.sentinel.spill_store

    def config(self, provider: str = "openai", model: str = "model-a") -> AppConfig:
        return AppConfig(
            workspace=self.workspace,
            provider=ProviderConfig(provider, "test-key", "https://example.test", model),
            extensions=ExtensionsConfig(enabled=True),
            mcp=MCPConfig(
                enabled=True,
                servers=(MCPServerConfig("docs", "stdio", "python", enabled=True),),
            ),
        )

    def active(self, record, memory, _options):  # type: ignore[no-untyped-def]
        journal = ChangeJournal()
        context = ToolContext(
            WorkspacePolicy(record.workspace),
            CommandPolicy(),
            lambda _action, _detail: False,
            change_journal=journal,
            spill_store=self.spill,  # type: ignore[arg-type]
        )
        tools = ToolRegistry(context)
        return ActiveSession(
            record,
            memory,
            SessionContext(persisted_summary=memory.summary),
            self.config(record.provider, record.model),
            FakeAgent(record.provider),
            tools,
            journal,
            self.audit,  # type: ignore[arg-type]
        )

    def manager(self, config, context, source_env, audit):  # type: ignore[no-untyped-def]
        return _TaskManager(self, config, context, source_env, audit)

    def agent(self, _provider, tools, **kwargs):  # type: ignore[no-untyped-def]
        owner = self

        class TaskAgent:
            def __init__(self) -> None:
                self.tools = tools
                self.kwargs = kwargs

            async def run_with_context_async(
                self,
                task: str,
                context: SessionContext,
                *,
                cancellation=None,  # type: ignore[no-untyped-def]
                event_sink=None,  # type: ignore[no-untyped-def]
            ) -> SessionTurnResult:
                owner.task_agents.append(self)
                self.task = task
                self.context = context
                self.cancellation = cancellation
                self.event_sink = event_sink
                self.definitions = tuple(definition.name for definition in tools.definitions)
                return SessionTurnResult(
                    RunResult(True, "MCP task", 1),
                    context,
                )

        return TaskAgent()
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

    def test_mcp_tasks_use_fresh_registry_manager_and_agent_without_mutating_session(self) -> None:
        """两个任务不得复用 MCP 连接或把动态工具写入长期会话。"""
        factory = MCPRuntimeFactory(self.workspace)
        source_env = {
            "TRICODER_EXTENSION_ENV_ALLOWLIST": "DOCS_MCP_TOKEN",
            "DOCS_MCP_TOKEN": "test-token",
        }
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ=source_env),
            active_session_factory=factory.active,
            provider_factory=lambda _config, _timeout: mock.sentinel.provider,
            agent_factory=factory.agent,
            mcp_manager_factory=factory.manager,
        )
        original = runtime.current
        builtin_names = tuple(definition.name for definition in original.tools.definitions)  # type: ignore[union-attr]

        runtime.run_task("first")
        runtime.run_task("second")

        managers = [manager for event, manager in factory.manager_events if event == "start"]
        self.assertEqual(2, len(managers))
        self.assertIsNot(managers[0], managers[1])
        self.assertTrue(all(manager.stopped for manager in managers))
        self.assertEqual(2, len(factory.task_agents))
        self.assertIsNot(factory.task_agents[0], factory.task_agents[1])
        self.assertIsNot(factory.task_agents[0].tools, factory.task_agents[1].tools)  # type: ignore[attr-defined]
        self.assertIn("mcp__docs__echo", factory.task_agents[0].definitions)  # type: ignore[attr-defined]
        self.assertIn("mcp__docs__echo", factory.task_agents[1].definitions)  # type: ignore[attr-defined]
        self.assertEqual(builtin_names, tuple(definition.name for definition in runtime.current.tools.definitions))  # type: ignore[union-attr]
        self.assertFalse(runtime.current.tools.contains("mcp__docs__echo"))  # type: ignore[union-attr]

        for manager in managers:
            self.assertIs(original.tools.context, manager.context)  # type: ignore[union-attr]
            self.assertIs(original.audit, manager.audit)
            self.assertEqual(source_env, manager.source_env)
        for task_agent in factory.task_agents:
            self.assertIs(original.context, task_agent.context)  # type: ignore[attr-defined]
            self.assertIs(original.tools.context.change_journal, task_agent.tools.context.change_journal)  # type: ignore[union-attr]
            self.assertIs(original.tools.context.spill_store, task_agent.tools.context.spill_store)  # type: ignore[union-attr]
            self.assertIs(original.tools.context.approver, task_agent.tools.context.approver)  # type: ignore[union-attr]

    def test_disabled_mcp_uses_existing_agent_and_never_loads_sdk(self) -> None:
        """关闭配置必须保持旧调用对象和惰性 SDK 边界。"""
        original_agent = self.runtime.current.agent
        with mock.patch(
            "tricoder.mcp.sdk.load_mcp_sdk",
            side_effect=AssertionError("disabled path loaded SDK"),
        ), mock.patch(
            "tricoder.session_runtime.run_mcp_task_sync",
            side_effect=AssertionError("disabled path entered MCP scope"),
            create=True,
        ):
            result = self.runtime.run_task("disabled")

        self.assertTrue(result.ok)
        self.assertIs(original_agent, self.runtime.current.agent)
        self.assertEqual(["disabled"], original_agent.calls)  # type: ignore[attr-defined]

    def test_mcp_runner_cancel_signals_command_before_cleanup_and_seals_journal(self) -> None:
        """真实 Runtime/Gateway 的 runner 取消须先通知命令线程，再清理并封存账本。"""
        factory = MCPRuntimeFactory(self.workspace)
        events = []
        original_errors = []
        worker_started = threading.Event()
        worker_cancelled = threading.Event()
        shared_tokens = []

        class Journal(CountingJournal):
            def seal_task(self, modified_files, verification):
                events.append(("seal", shared_tokens[0].is_cancelled))
                return super().seal_task(modified_files, verification)

        journal = Journal()

        class Manager:
            async def start_all(self, cancellation):
                shared_tokens.append(cancellation)

            def register_tools(self, registry):
                return 0

            def unregister_tools(self, registry):
                events.append(("unregister", shared_tokens[0].is_cancelled))
                return 0

            async def stop_all(self):
                events.append(("stop", shared_tokens[0].is_cancelled))

        class Agent:
            def __init__(self, provider, tools, **kwargs):
                self.tools = tools

            async def run_with_context_async(self, task, context, *, cancellation, **kwargs):
                written = await self.tools.execute_async(
                    "create_file", {"path": "cancelled.txt", "content": "controlled change"},
                    cancellation=cancellation,
                )
                if not written.ok:
                    raise AssertionError(written.output)
                command = asyncio.create_task(self.tools.execute_async(
                    "run_command", {"command": "python -m unittest"}, cancellation=cancellation,
                ))
                while not worker_started.is_set():
                    await asyncio.sleep(0)
                asyncio.current_task().cancel("runner-cancel")
                try:
                    await command
                except asyncio.CancelledError as exc:
                    original_errors.append(exc)
                    raise

        def controlled_process(args, *, cancellation, **kwargs):
            # 仅替换操作系统进程层，其上的审批、网关和线程桥接均使用真实代码。
            worker_started.set()
            if cancellation.wait(timeout=1.0):
                worker_cancelled.set()
            raise CancellationError("controlled command stopped")

        runtime = SessionRuntime(
            self.store, self.workspace, options=RuntimeOptions(environ={}),
            active_session_factory=factory.active,
            provider_factory=lambda *_args: object(), agent_factory=Agent,
            mcp_manager_factory=lambda *_args: Manager(),
        )
        runtime.current = replace(runtime.current, journal=journal)
        runtime.current.tools.context.change_journal = journal
        runtime.current.tools.context.approver = lambda *_args: True
        with mock.patch("tricoder.tools.command.run_bounded_process", side_effect=controlled_process):
            with self.assertRaises(asyncio.CancelledError) as caught:
                runtime.run_task("cancel running command")
        self.assertEqual([("unregister", True), ("stop", True), ("seal", True)], events)
        self.assertTrue(worker_cancelled.is_set())
        self.assertIs(original_errors[0], caught.exception)
        self.assertEqual((1, 1), (journal.begun, journal.sealed))
        self.assertIsNotNone(journal.latest())
        self.assertFalse(runtime.cancel_current())

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

    def test_preview_conflict_reports_only_canonical_paths(self) -> None:
        """防止首次撤销冲突被改写成无路径泛化错误或泄漏底层状态。"""
        runtime, audit_path = self._runtime_with_real_registry()
        runtime.run_task("write mixed change")
        sentinel = "PREVIEW-PRIVATE-CONFLICT-SENTINEL-5F10"
        created = self.workspace / "src" / "created.py"
        created.write_text(f"{sentinel}\n", encoding="utf-8")

        with self.assertRaises(SessionRuntimeError) as captured:
            runtime.prepare_undo()

        self.assertEqual(
            "无法撤销，文件状态冲突：src/created.py",
            str(captured.exception),
        )
        self.assertNotIn(sentinel, str(captured.exception))
        self.assertNotIn(str(self.workspace), str(captured.exception))
        serialized = audit_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, serialized)
        event = json.loads(serialized.splitlines()[-1])
        self.assertEqual("undo", event["event"])
        self.assertEqual("conflicted", event["status"])
        self.assertEqual(["src/app.py", "src/created.py"], event["paths"])
        self.assertEqual(2, event["file_count"])
        self.assertEqual(1, event["conflict_count"])
        self.assertEqual("not-required", event["compensation_status"])

    def test_failed_undo_execution_audits_conflict_without_private_state(self) -> None:
        """防止确认后的第二次校验冲突没有结构化失败审计。"""
        runtime, audit_path = self._runtime_with_real_registry()
        runtime.run_task("write mixed change")
        runtime.prepare_undo()
        sentinel = "EXECUTION-PRIVATE-CONFLICT-SENTINEL-8C22"
        created = self.workspace / "src" / "created.py"
        created.write_text(f"{sentinel}\n", encoding="utf-8")

        execution = runtime.undo_latest()

        self.assertFalse(execution.ok)
        serialized = audit_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, serialized)
        event = json.loads(serialized.splitlines()[-1])
        self.assertEqual("undo", event["event"])
        self.assertEqual("conflicted", event["status"])
        self.assertEqual(["src/created.py"], event["conflicts"])
        self.assertEqual(1, event["conflict_count"])
        self.assertEqual("not-required", event["compensation_status"])

    def test_failed_undo_execution_audits_compensation_failure_paths(self) -> None:
        """防止部分撤销补偿失败缺少安全结构化事件。"""
        runtime, audit_path = self._runtime_with_real_registry()
        runtime.run_task("write mixed change")

        class CompensationFailingTools:
            def undo_change_set(self, _change_set) -> UndoExecution:  # type: ignore[no-untyped-def]
                return UndoExecution(
                    False,
                    ("src/app.py", "src/created.py"),
                    compensation_failed=("src/app.py",),
                )

        runtime.current = replace(
            runtime.current,
            tools=CompensationFailingTools(),  # type: ignore[arg-type]
        )

        execution = runtime.undo_latest()

        self.assertFalse(execution.ok)
        event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("undo", event["event"])
        self.assertEqual("failed", event["status"])
        self.assertEqual(["src/app.py"], event["compensation_failed"])
        self.assertEqual("failed", event["compensation_status"])
        self.assertEqual(0, event["conflict_count"])

    def test_undo_execution_exception_is_safely_audited_and_wrapped(self) -> None:
        """防止最终撤销打开/核验异常越过运行时且漏记失败事件。"""
        runtime, audit_path = self._runtime_with_real_registry()
        runtime.run_task("write mixed change")
        sentinel = "UNDO-EXECUTION-PRIVATE-ERROR-SENTINEL-0A19"
        absolute = str(self.workspace)

        class ExplodingTools:
            def undo_change_set(self, _change_set) -> UndoExecution:  # type: ignore[no-untyped-def]
                raise OSError(f"{absolute} {sentinel}")

        runtime.current = replace(
            runtime.current,
            tools=ExplodingTools(),  # type: ignore[arg-type]
        )

        with self.assertRaises(SessionRuntimeError) as captured:
            runtime.undo_latest()

        self.assertEqual("无法安全执行最近任务的撤销", str(captured.exception))
        self.assertNotIn(sentinel, str(captured.exception))
        serialized = audit_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, serialized)
        event = json.loads(serialized.splitlines()[-1])
        self.assertEqual("undo", event["event"])
        self.assertEqual("failed", event["status"])
        self.assertEqual(["src/app.py", "src/created.py"], event["paths"])
        self.assertEqual(2, event["file_count"])
        self.assertEqual(0, event["conflict_count"])
        self.assertEqual("not-required", event["compensation_status"])

    def test_diff_rejects_tainted_change_set_without_rendering_snapshots(self) -> None:
        """防止失去所有权证明的任务继续回显账本源码。"""
        journal = ChangeJournal()
        before = FileSnapshot("src/app.py", "before-private\n", 0o644, FileIdentity(1, 1))
        after = FileSnapshot("src/app.py", "after-private\n", 0o644, FileIdentity(1, 2))
        journal.begin_task((), "not-run")
        journal.record_committed("src/app.py", before, after)
        journal.mark_tainted("src/app.py")
        journal.seal_task(("src/app.py",), "not-run")
        self.runtime.current = replace(self.runtime.current, journal=journal)

        with self.assertRaises(SessionRuntimeError) as captured:
            self.runtime.diff_latest()

        self.assertEqual(
            "无法显示，任务文件状态冲突：src/app.py",
            str(captured.exception),
        )
        self.assertNotIn("before-private", str(captured.exception))
        self.assertNotIn("after-private", str(captured.exception))

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

    def test_default_runtime_binds_spill_to_state_directory_and_clear_cleans_it(self) -> None:
        """spill 不能落在源码工作区，/clear 必须结束当前会话结果生命周期。"""
        audit_dir = (self.root / "audit").resolve()
        stale_store = ToolResultSpillStore(
            self.store.database_path.parent / "runtime" / "tool-results",
            self.first.id,
        )
        stale_record = stale_store.persist("old-process-call", "stale body")

        def config_loader(**kwargs):  # type: ignore[no-untyped-def]
            provider = kwargs.get("provider") or "openai"
            model = kwargs.get("model") or "model-a"
            return AppConfig(
                workspace=Path(kwargs["workspace"]).resolve(),
                provider=ProviderConfig(
                    provider,
                    "test-key",
                    "https://example.test",
                    model,
                ),
                audit_dir=audit_dir,
            )

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=config_loader,
            provider_factory=lambda _config, _timeout: object(),  # type: ignore[arg-type]
            agent_factory=lambda _provider, _tools, **_kwargs: FakeAgent("openai"),
        )
        spill = runtime.current.tools.context.spill_store  # type: ignore[union-attr]
        self.assertIsNotNone(spill)
        assert spill is not None
        self.assertTrue(spill.root.is_relative_to(self.store.database_path.parent))
        self.assertFalse(spill.root.is_relative_to(self.workspace))
        with self.assertRaisesRegex(OSError, "引用"):
            stale_store.preview(stale_record.reference)
        record = spill.persist("call-a", "temporary body")

        runtime.clear_current()

        with self.assertRaisesRegex(OSError, "引用"):
            spill.preview(record.reference)

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

    def test_failed_clear_still_removes_spilled_tool_results(self) -> None:
        """即使 SQLite 暂时不可写，/clear 也必须立即销毁临时工具正文。"""
        wrapped_store = FailingMemoryStore(self.store)
        audit_dir = (self.root / "audit").resolve()

        def config_loader(**kwargs):  # type: ignore[no-untyped-def]
            return AppConfig(
                workspace=Path(kwargs["workspace"]).resolve(),
                provider=ProviderConfig(
                    kwargs.get("provider") or "openai",
                    "test-key",
                    "https://example.test",
                    kwargs.get("model") or "model-a",
                ),
                audit_dir=audit_dir,
            )

        runtime = SessionRuntime(
            wrapped_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=config_loader,
            provider_factory=lambda _config, _timeout: object(),  # type: ignore[arg-type]
            agent_factory=lambda _provider, _tools, **_kwargs: FakeAgent("openai"),
        )
        spill = runtime.current.tools.context.spill_store  # type: ignore[union-attr]
        assert spill is not None
        runtime.run_task("先写入一段可清空的会话摘要")
        record = spill.persist("call-a", "temporary body")
        wrapped_store.fail_writes = True

        with self.assertRaises(SessionRuntimeError):
            runtime.clear_current()

        with self.assertRaisesRegex(OSError, "引用"):
            spill.preview(record.reference)

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

    def test_permission_level_defaults_strict_and_validates(self) -> None:
        """权限级别默认 strict，非法值被拒绝。"""
        self.assertEqual("strict", self.runtime.permission_level)
        with self.assertRaises(SessionRuntimeError):
            self.runtime.set_permission("admin")
        self.assertEqual("strict", self.runtime.set_permission(None))

    def test_permission_relaxed_does_not_auto_approve_code_commands(self) -> None:
        """relaxed 不自动放行任何命令；代码/测试/脚本执行仍走人工审批。"""
        self.runtime.set_permission("relaxed")
        self.assertEqual("relaxed", self.runtime.permission_level)
        # relaxed 下 run_command 一律人工审批（不再放行 Python/测试/脚本）
        self.assertFalse(self.runtime._effective_approver("run_command", "detail"))
        self.assertFalse(self.runtime._effective_approver("edit_file", "detail"))
        self.assertFalse(self.runtime._effective_approver("create_file", "detail"))
        self.assertFalse(self.runtime._effective_approver("apply_patch", "detail"))
        # strict 下同样人工审批
        self.runtime.set_permission("strict")
        self.assertFalse(self.runtime._effective_approver("run_command", "detail"))

    def test_permission_relaxed_auto_approves_git_only(self) -> None:
        """relaxed 只自动放行不返回文件内容的 Git 元数据查询。"""
        self.runtime.set_permission("relaxed")
        self.assertTrue(
            self.runtime._auto_approve_git_command(
                ["C:\\git.exe", "status", "--short"]
            )
        )
        self.assertTrue(
            self.runtime._auto_approve_git_command(
                ["C:\\git.exe", "diff", "--stat"]
            )
        )
        self.assertFalse(
            self.runtime._auto_approve_git_command(
                ["C:\\git.exe", "show", "HEAD:.env.local"]
            )
        )
        self.assertFalse(
            self.runtime._auto_approve_git_command(
                ["C:\\git.exe", "status", "--verbose"]
            )
        )
        self.assertFalse(
            self.runtime._auto_approve_git_command(
                ["C:\\python.exe", "-m", "unittest"]
            )
        )
        self.assertFalse(
            self.runtime._auto_approve_git_command(
                ["C:\\python.exe", "test/script.py"]
            )
        )
        self.runtime.set_permission("strict")
        self.assertFalse(
            self.runtime._auto_approve_git_command(["C:\\git.exe", "status"])
        )

    def test_permission_fullaccess_allows_all_but_dangerous_tools(self) -> None:
        """fullaccess 放行全部现有工具（明确非沙盒）；危险工具集合仍审批。"""
        self.runtime.set_permission("fullaccess")
        self.assertEqual("fullaccess", self.runtime.permission_level)
        for action in ("edit_file", "create_file", "apply_patch", "run_command"):
            with self.subTest(action=action):
                self.assertTrue(self.runtime._effective_approver(action, "detail"))
        # 非法值仍拒绝
        with self.assertRaises(SessionRuntimeError):
            self.runtime.set_permission("admin")

    def test_mcp_server_start_always_uses_human_approval(self) -> None:
        """MCP server 拥有进程权限，strict/relaxed/fullaccess 均不得自动放行。"""
        approvals: list[tuple[str, str]] = []

        def reject(action: str, detail: str) -> bool:
            approvals.append((action, detail))
            return False

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
            approver=reject,
        )

        for level in ("strict", "relaxed", "fullaccess"):
            with self.subTest(level=level):
                runtime.set_permission(level)
                self.assertFalse(
                    runtime._effective_approver(
                        "dangerous_mcp_server_start",
                        "server",
                    )
                )

        self.assertEqual(3, len(approvals))
        self.assertTrue(
            all(action == "dangerous_mcp_server_start" for action, _ in approvals)
        )

    def test_permission_persists_to_session_memory(self) -> None:
        """权限级别随会话记忆持久化并可恢复。"""
        self.assertEqual("strict", self.runtime.permission_level)
        self.runtime.set_permission("relaxed")
        self.assertEqual("relaxed", self.runtime.permission_level)
        persisted = self.store.load_memory(self.runtime.current.record.id)
        self.assertEqual("relaxed", persisted.permission_level)

    def test_set_permission_reports_persist_failure(self) -> None:
        """安全权限持久化失败时必须回滚内存中的权限。"""
        failing = FailingMemoryStore(self.store)
        failing.fail_writes = True
        runtime = SessionRuntime(
            failing,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=FakeBuilder(set()),
        )
        with self.assertRaises(SessionRuntimeError):
            runtime.set_permission("relaxed")
        self.assertEqual("strict", runtime.permission_level)
        self.assertEqual(
            "strict",
            self.store.load_memory(runtime.current.record.id).permission_level,
        )

    def test_session_switch_and_task_start_are_atomic(self) -> None:
        """会话候选仍在构建时，任务不得穿过空闲检查并开始执行。"""
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def blocking_builder(record, memory, options):  # type: ignore[no-untyped-def]
            if record.id == self.second.id:
                entered.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("测试未及时释放会话构建")
            return self.builder(record, memory, options)

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=blocking_builder,
        )

        def switch_session() -> None:
            try:
                runtime.switch(self.second.id, confirm=lambda _workspace: True)
            except BaseException as exc:  # pragma: no cover - 仅用于跨线程回传
                errors.append(exc)

        worker = threading.Thread(target=switch_session)
        worker.start()
        self.assertTrue(entered.wait(timeout=2))
        try:
            with self.assertRaises(SessionRuntimeError):
                runtime.run_task("不得与会话切换并发")
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_run_task_lock_rejects_second_task_and_config_changes(self) -> None:
        """同一时刻只允许一个任务；任务运行期间拒绝权限/会话修改。"""
        self.runtime._task_lock.acquire()
        self.runtime._task_active = True
        self.runtime._task_session_id = "occupied"
        self.runtime._task_permission = "strict"
        try:
            with self.assertRaises(SessionRuntimeError):
                self.runtime.run_task("second")
            with self.assertRaises(SessionRuntimeError):
                self.runtime.set_permission("relaxed")
            with self.assertRaises(SessionRuntimeError):
                self.runtime.switch(self.second.id, confirm=lambda w: True)
            with self.assertRaises(SessionRuntimeError):
                self.runtime.change_model("glm")
        finally:
            self.runtime._task_active = False
            self.runtime._task_session_id = None
            self.runtime._task_permission = None
            self.runtime._task_lock.release()
        # 释放后可正常操作
        self.runtime.set_permission("relaxed")
        self.assertEqual("relaxed", self.runtime.permission_level)

    def test_cancel_current_signals_running_task_without_taking_task_lock(self) -> None:
        """防止 UI 取消被运行任务持有的状态锁阻塞。"""
        agent = BlockingCancellableAgent()
        self.runtime.current = replace(self.runtime.current, agent=agent)
        self.runtime._cache_current()
        results: list[RunResult] = []

        worker = threading.Thread(target=lambda: results.append(self.runtime.run_task("task")))
        worker.start()
        self.assertTrue(agent.started.wait(timeout=2))

        self.assertTrue(self.runtime.cancel_current())
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(agent.cancelled.is_set())
        self.assertFalse(results[0].ok)
        self.assertFalse(self.runtime.cancel_current())

    def test_cancel_current_cannot_miss_task_during_token_publication(self) -> None:
        """任务已声明活动但令牌尚未发布时，取消调用必须等待并命中新任务。"""
        constructing = threading.Event()
        allow_construction = threading.Event()

        class SlowToken(CancellationToken):
            def __init__(self) -> None:
                constructing.set()
                if not allow_construction.wait(timeout=2):
                    raise TimeoutError("测试未允许令牌构造")
                super().__init__()

        agent = BlockingCancellableAgent()
        self.runtime.current = replace(self.runtime.current, agent=agent)
        self.runtime._cache_current()
        results: list[RunResult] = []
        cancellation_results: list[bool] = []

        with mock.patch("tricoder.session_runtime.CancellationToken", SlowToken):
            worker = threading.Thread(
                target=lambda: results.append(self.runtime.run_task("task"))
            )
            worker.start()
            self.assertTrue(constructing.wait(timeout=2))
            canceller = threading.Thread(
                target=lambda: cancellation_results.append(
                    self.runtime.cancel_current()
                )
            )
            canceller.start()
            time.sleep(0.05)
            allow_construction.set()
            canceller.join(timeout=2)
            worker.join(timeout=2)

        self.assertFalse(canceller.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual([True], cancellation_results)
        self.assertFalse(results[0].ok)


if __name__ == "__main__":
    unittest.main()
