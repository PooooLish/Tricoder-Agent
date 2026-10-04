"""无持久化入口、延迟创建和显式恢复的行为验收。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tricoder.config import ConfigError
from tricoder.models import (
    AppConfig,
    Message,
    ProviderConfig,
    RunResult,
    SessionContext,
    SessionTurnResult,
)
from tricoder.session.lock import SessionLock
from tricoder.session.runtime import (
    ActiveSession,
    RuntimeOptions,
    SessionRuntime,
    SessionRuntimeError,
)
from tricoder.session.store import SessionStore


class _FakeAgent:
    """入口测试不会调用真实 Provider。"""

    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self.failure = failure

    def run_with_context(
        self,
        task: str,
        context: SessionContext,
        **_kwargs,
    ) -> SessionTurnResult:
        self.calls.append(task)
        if self.failure is not None:
            raise self.failure
        return SessionTurnResult(
            RunResult(True, "synthetic-complete", 1, verification="not-run"),
            SessionContext(
                messages=context.messages + (Message("user", task, kind="task"),),
                persisted_summary=context.persisted_summary,
                verification="not-run",
            ),
        )


class _CountingBuilder:
    def __init__(
        self,
        agent: _FakeAgent | None = None,
        *,
        before_return=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.built_ids: list[str] = []
        self.options: list[RuntimeOptions] = []
        self.agent = agent or _FakeAgent()
        self.before_return = before_return

    def __call__(self, record, memory, options):  # type: ignore[no-untyped-def]
        self.built_ids.append(record.id)
        self.options.append(options)
        if self.before_return is not None:
            self.before_return()
        config = AppConfig(
            workspace=record.workspace,
            provider=ProviderConfig(
                record.provider,
                "synthetic-key",
                "https://example.invalid",
                record.model,
            ),
        )
        return ActiveSession(
            record,
            memory,
            SessionContext(persisted_summary=memory.summary),
            config,
            self.agent,
        )


class SessionLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        self.store = SessionStore(self.root / "state" / "sessions.db")
        self.store.initialize(self.workspace)

    def _entry(self, builder: _CountingBuilder | None = None) -> SessionRuntime:
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=builder or _CountingBuilder(),
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_startup_stays_unbound_even_when_latest_is_occupied(self) -> None:
        """默认入口若误恢复 latest，就会争抢真实会话锁并触发候选构建。"""

        record = self.store.create(
            "existing",
            self.workspace,
            "openai",
            "model-a",
        )
        owner = SessionLock(self.store.database_path, record.id)
        self.addCleanup(owner.close)
        before = self.store.list_all()
        first_builder = _CountingBuilder()
        second_builder = _CountingBuilder()

        first = self._entry(first_builder)
        second = self._entry(second_builder)

        self.assertFalse(first.has_active_session)
        self.assertFalse(second.has_active_session)
        self.assertIsNone(first.current)
        self.assertIsNone(second.current)
        self.assertEqual(before, self.store.list_all())
        self.assertEqual([], first_builder.built_ids)
        self.assertEqual([], second_builder.built_ids)

    def test_explicit_resume_locks_exact_id(self) -> None:
        """显式恢复必须锁定给定 UUID，不能退回工作区中更新时间更晚的记录。"""

        exact = self.store.create("exact", self.workspace, "openai", "model-a")
        latest = self.store.create("latest", self.workspace, "glm", "model-b")
        builder = _CountingBuilder()
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=builder,
            initial_session_id=exact.id,
        )
        self.addCleanup(runtime.close)

        self.assertTrue(runtime.has_active_session)
        self.assertIsNotNone(runtime.current)
        assert runtime.current is not None
        self.assertEqual(exact.id, runtime.current.record.id)
        self.assertNotEqual(latest.id, runtime.current.record.id)
        self.assertEqual([exact.id], builder.built_ids)

        with self.assertRaisesRegex(Exception, "占用"):
            SessionRuntime(
                self.store,
                self.workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=_CountingBuilder(),
                initial_session_id=exact.id,
            )

    def test_empty_entry_close_is_idempotent(self) -> None:
        """关闭未激活入口不能创建或持久化伪会话。"""

        runtime = self._entry()

        self.assertTrue(runtime.close())
        self.assertTrue(runtime.close())
        self.assertFalse(runtime.has_active_session)
        self.assertIsNone(runtime.status().record)
        self.assertEqual([], self.store.list_all())

    def test_first_task_creates_once_and_is_delivered_once(self) -> None:
        """若首任务重复创建或重放，记录数、ID 或 Agent 调用序列会偏离。"""

        agent = _FakeAgent()
        runtime = self._entry(_CountingBuilder(agent))

        first = runtime.run_task("first task")
        first_id = runtime._require_active_session().record.id
        second = runtime.run_task("second task")

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(first_id, runtime._require_active_session().record.id)
        self.assertEqual(["first task", "second task"], agent.calls)
        self.assertEqual(1, len(self.store.list_all()))

    def test_first_task_inherits_entry_options_and_pending_permission(self) -> None:
        """延迟创建不能丢失启动边界或入口态中明确选择的权限。"""

        builder = _CountingBuilder()
        options = RuntimeOptions(
            environ={},
            provider="glm",
            model="glm-test",
            max_rounds=7,
            max_context_chars=4321,
            read_only=True,
        )
        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=options,
            active_session_factory=builder,
        )
        self.addCleanup(runtime.close)

        runtime.set_permission("relaxed")
        runtime.run_task("synthetic task")

        active = runtime._require_active_session()
        self.assertEqual(self.workspace, active.record.workspace)
        self.assertEqual(("glm", "glm-test"), (active.record.provider, active.record.model))
        self.assertEqual("relaxed", active.memory.permission_level)
        self.assertEqual([options], builder.options)
        self.assertTrue(builder.options[0].read_only)
        self.assertEqual(7, builder.options[0].max_rounds)
        self.assertEqual(4321, builder.options[0].max_context_chars)

    def test_entry_provider_changes_keep_each_provider_model_preview_distinct(self) -> None:
        """待用 Provider 连续切换时，前一个模型名不能污染其他 Provider。"""

        runtime = SessionRuntime(
            self.store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_CountingBuilder(),
            model_preview_resolver=lambda *_args, **kwargs: {
                provider: kwargs.get("model") or default
                for provider, default in {
                    "openai": "openai-preview",
                    "deepseek": "deepseek-preview",
                    "glm": "glm-preview",
                }.items()
            },
        )
        self.addCleanup(runtime.close)

        runtime.change_model("deepseek")
        self.assertEqual("deepseek-preview", runtime.status().model)
        runtime.change_model("glm")

        self.assertEqual("glm", runtime.status().provider)
        self.assertEqual("glm-preview", runtime.status().model)
        self.assertEqual([], self.store.list_all())

    def test_auto_name_does_not_persist_prompt(self) -> None:
        """自动标题若引用首条任务，会把合成敏感正文写入 SQLite。"""

        database = self.root / "named-state" / "sessions.db"
        fixed_id = "8a21d3f0-1111-4222-8333-1234567890ab"
        store = SessionStore(
            database,
            clock=lambda: "2026-09-28T17:30:05+00:00",
            id_factory=lambda: fixed_id,
        )
        runtime = SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_CountingBuilder(),
        )
        self.addCleanup(runtime.close)
        prompt = "SYNTHETIC-SECRET-MARKER\n" + "print('private')\n" * 100

        runtime.run_task(prompt)

        record = runtime._require_active_session().record
        self.assertEqual("会话-20260928-173005-8a21d3f0", record.name)
        self.assertNotIn("SYNTHETIC", record.name)
        self.assertNotIn("private", record.name)
        self.assertLessEqual(len(record.name), 50)
        self.assertEqual(record.name, store.get(fixed_id).name)

    def test_two_entries_with_same_first_task_get_distinct_ids(self) -> None:
        """同秒同任务的两个入口必须各自创建 UUID，且真实会话继续互斥。"""

        ids = iter(
            (
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
        )
        store = SessionStore(
            self.root / "two-entry-state" / "sessions.db",
            clock=lambda: "2026-09-28T17:30:05+00:00",
            id_factory=lambda: next(ids),
        )
        first = SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_CountingBuilder(),
        )
        second = SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_CountingBuilder(),
        )
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        first.run_task("same task")
        second.run_task("same task")
        first_id = first._require_active_session().record.id
        second_id = second._require_active_session().record.id

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(2, len(store.list_all()))
        with self.assertRaisesRegex(SessionRuntimeError, "占用"):
            SessionRuntime(
                store,
                self.workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=_CountingBuilder(),
                initial_session_id=first_id,
            )
        with self.assertRaisesRegex(SessionRuntimeError, "占用"):
            SessionRuntime(
                store,
                self.workspace,
                options=RuntimeOptions(environ={}),
                active_session_factory=_CountingBuilder(),
                initial_session_id=second_id,
            )

    def test_concurrent_first_submission_and_shutdown_does_not_activate_late(self) -> None:
        """关闭在候选构建中到达时，不能迟到插入会话或执行首次任务。"""

        entered = threading.Event()
        release = threading.Event()
        agent = _FakeAgent()

        def block_build() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release builder")

        runtime = self._entry(_CountingBuilder(agent, before_return=block_build))
        errors: list[BaseException] = []

        def submit() -> None:
            try:
                runtime.run_task("first task")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=submit)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(SessionRuntimeError, "正在运行"):
                runtime.run_task("second task")
            self.assertTrue(runtime.cancel_current())
            self.assertFalse(runtime.close())
        finally:
            release.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], agent.calls)
        self.assertIsNone(runtime.current)
        self.assertEqual([], self.store.list_all())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], SessionRuntimeError)

    def test_precommit_failures_keep_entry_but_provider_failure_keeps_session(self) -> None:
        """配置/构建/插入失败不可留行；提交后的执行失败必须保留真实 ID。"""

        def fail_config(**_kwargs):  # type: ignore[no-untyped-def]
            raise ConfigError("synthetic configuration failure")

        config_runtime = SessionRuntime(
            SessionStore(self.root / "config-failure" / "sessions.db"),
            self.workspace,
            options=RuntimeOptions(environ={}),
            config_loader=fail_config,
        )
        self.addCleanup(config_runtime.close)
        with self.assertRaises(SessionRuntimeError):
            config_runtime.run_task("first")
        self.assertIsNone(config_runtime.current)
        self.assertEqual([], config_runtime.store.list_all())

        insert_store = SessionStore(self.root / "insert-failure" / "sessions.db")
        insert_runtime = SessionRuntime(
            insert_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_CountingBuilder(),
        )
        self.addCleanup(insert_runtime.close)
        with mock.patch.object(
            insert_store,
            "insert_prepared",
            side_effect=OSError("synthetic insert failure"),
        ):
            with self.assertRaises(SessionRuntimeError):
                insert_runtime.run_task("first")
        self.assertIsNone(insert_runtime.current)
        self.assertEqual([], insert_store.list_all())

        provider_error = RuntimeError("synthetic provider failure")
        provider_store = SessionStore(self.root / "provider-failure" / "sessions.db")
        provider_runtime = SessionRuntime(
            provider_store,
            self.workspace,
            options=RuntimeOptions(environ={}),
            active_session_factory=_CountingBuilder(_FakeAgent(failure=provider_error)),
        )
        self.addCleanup(provider_runtime.close)
        with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
            provider_runtime.run_task("first")
        self.assertTrue(provider_runtime.has_active_session)
        self.assertEqual(1, len(provider_store.list_all()))


if __name__ == "__main__":
    unittest.main()
