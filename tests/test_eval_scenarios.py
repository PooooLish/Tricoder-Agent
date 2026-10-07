"""Production-path multi-turn and reviewed-memory Eval scenarios."""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from tricoder.agent import CodingAgent
from tricoder.context.memory import ConversationMemory, MemoryItem
from tricoder.context.summarizer import MemorySummaryResult
from tricoder.evals.models import ScenarioStep
from tricoder.evals.scenarios import run_agent_turns, run_runtime_scenario
from tricoder.evals.loader import EvalDefinitionError, load_suite
from tricoder.models import (
    AppConfig,
    MemoryConfig,
    ProviderConfig,
    ProviderResponse,
    SessionContext,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tricoder.session.runtime import ActiveSession, RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore


class _RecordingFinishProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        del tools
        self.requests.append(
            tuple(
                message.content or ""
                for message in messages
                if message.role == "user"
            )
        )
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    f"finish-{len(self.requests)}",
                    "finish",
                    {"summary": "synthetic done"},
                ),
            ),
            finish_reason="tool_calls",
            usage=TokenUsage(input_tokens=5, output_tokens=1),
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
        del name, kwargs
        return ToolResult(True, str(arguments.get("summary", "")))


class _ConstraintSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, previous, source, cancellation):  # type: ignore[no-untyped-def]
        del cancellation
        self.calls += 1
        task = next(message for message in source if message.kind == "task")
        source_id = f"m{task.message_seq}"
        covered = max(message.message_seq or 0 for message in source)
        text = (
            "使用新的蓝色约束"
            if "改为蓝色" in (task.content or "")
            else "保持红色约束"
        )
        candidate = ConversationMemory(
            revision=previous.revision,
            generation=previous.generation,
            covered_through=covered,
            constraints=(MemoryItem("color-rule", text, (source_id,), "session"),),
        )
        return MemorySummaryResult(
            candidate,
            TokenUsage(input_tokens=3, output_tokens=2),
        )


class EvalScenarioTests(unittest.TestCase):
    def test_v2_suite_loads_only_registered_steps_and_rejects_control_content(self) -> None:
        """Control actions are trusted fixture data and can never carry a model prompt."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_dir = root / "cases" / "memory-case"
            (case_dir / "workspace").mkdir(parents=True)
            (case_dir / "verifier").mkdir()
            (root / "suite.toml").write_text(
                'schema_version = 2\nbenchmark_version = "quality-v1"\n'
                'id = "quality"\ntitle = "Quality"\ncases = ["memory-case"]\n',
                encoding="utf-8",
            )
            case_toml = case_dir / "case.toml"
            case_toml.write_text(
                'id = "memory-case"\ntitle = "Memory"\ntask = "first"\n'
                'allowed_changes = ["app.py"]\nrequired_changes = ["app.py"]\n'
                'max_rounds = 4\nmax_context_chars = 4000\n'
                'category = "memory"\nsplit = "dev"\nexecution_kind = "quality"\n'
                'scorers = ["behavior_assertions"]\nfaults = ["none"]\n'
                'dimensions = ["memory_constraint_preserved", "agent_completed"]\n\n'
                '[[steps]]\nkind = "user_turn"\ncontent = "先保持红色"\n\n'
                '[[steps]]\nkind = "memory_save"\n\n'
                '[[verification]]\nname = "hidden"\n'
                'command = "python -m unittest discover -s .tricoder_eval_verifier -q"\n'
                'timeout = 5\n',
                encoding="utf-8",
            )

            suite = load_suite(root)

            self.assertEqual(2, suite.schema_version)
            self.assertEqual(("user_turn", "memory_save"), tuple(step.kind for step in suite.cases[0].steps))

            case_toml.write_text(
                case_toml.read_text("utf-8").replace(
                    'kind = "memory_save"',
                    'kind = "memory_save"\ncontent = "SEND THIS TO MODEL"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvalDefinitionError, "控制步骤|字段"):
                load_suite(root)

    def test_context_is_shared_inside_one_trial_and_empty_in_the_next(self) -> None:
        """Using agent.run for every turn would lose the first turn's context."""
        provider = _RecordingFinishProvider()
        agent = CodingAgent(provider, _FinishTools(), plan_enabled=False)

        observation = run_agent_turns(agent, ("先保持红色", "现在改为蓝色"))

        self.assertTrue(observation.result.ok)
        self.assertTrue(any("先保持红色" in item for item in provider.requests[1]))
        self.assertTrue(any("现在改为蓝色" in item for item in provider.requests[1]))

        next_provider = _RecordingFinishProvider()
        next_agent = CodingAgent(next_provider, _FinishTools(), plan_enabled=False)
        run_agent_turns(next_agent, ("独立任务",))
        self.assertFalse(any("先保持红色" in item for item in next_provider.requests[0]))

    def test_memory_switch_reaches_coding_agent_and_records_real_summary_calls(self) -> None:
        """An on/off label is invalid unless the production summarizer path actually changes."""
        off_summarizer = _ConstraintSummarizer()
        off_agent = CodingAgent(
            _RecordingFinishProvider(),
            _FinishTools(),
            plan_enabled=False,
            memory_config=MemoryConfig(compaction="off", persistence="off"),
            memory_summarizer=off_summarizer,
        )
        off = run_agent_turns(off_agent, ("先保持红色", "现在改为蓝色"))

        on_summarizer = _ConstraintSummarizer()
        on_agent = CodingAgent(
            _RecordingFinishProvider(),
            _FinishTools(),
            plan_enabled=False,
            max_context_chars=100_000,
            memory_config=MemoryConfig(
                compaction="structured",
                persistence="reviewed_summary",
            ),
            memory_summarizer=on_summarizer,
        )
        on = run_agent_turns(on_agent, ("先保持红色", "现在改为蓝色"))

        self.assertEqual(0, off.memory_calls)
        self.assertEqual(0, off_summarizer.calls)
        self.assertEqual(2, on.memory_calls)
        self.assertEqual(2, on_summarizer.calls)
        self.assertEqual(6, on.memory_usage.input_tokens)
        self.assertEqual(4, on.memory_usage.output_tokens)
        candidate = on.context.review_memory_candidate
        self.assertIsInstance(candidate, ConversationMemory)
        self.assertEqual("使用新的蓝色约束", candidate.constraints[0].text)

    def test_save_and_restart_use_real_session_runtime_public_apis(self) -> None:
        """Eval persistence scenarios must not write SQLite rows behind Runtime's back."""
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
            root = Path(temporary)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            database = (root / "state" / "sessions.db").resolve()
            summarizer = _ConstraintSummarizer()
            ids = iter(("session-main", "session-newer"))

            def runtime_factory(initial_session_id: str | None = None) -> SessionRuntime:
                store = SessionStore(database, id_factory=lambda: next(ids))
                if initial_session_id is not None:
                    store.initialize(workspace)
                    store.create("newer", workspace, "openai", "offline")

                def active_factory(record, memory, options):  # type: ignore[no-untyped-def]
                    del options
                    config = AppConfig(
                        workspace=workspace,
                        provider=ProviderConfig(
                            "openai", "synthetic", "https://example.invalid/v1", "offline"
                        ),
                        audit_dir=(root / "audit").resolve(),
                        memory=MemoryConfig(
                            compaction="structured",
                            persistence="reviewed_summary",
                        ),
                    )
                    agent = CodingAgent(
                        _RecordingFinishProvider(),
                        _FinishTools(),
                        plan_enabled=False,
                        memory_config=config.memory,
                        memory_summarizer=summarizer,
                    )
                    return ActiveSession(
                        record,
                        memory,
                        SessionContext(persisted_summary=memory.summary),
                        config,
                        agent,
                    )

                return SessionRuntime(
                    store,
                    workspace,
                    options=RuntimeOptions(environ={}),
                    active_session_factory=active_factory,
                    initial_session_id=initial_session_id,
                    workspace_confirmer=lambda _preview: True,
                )

            observation = run_runtime_scenario(
                (
                    ScenarioStep("user_turn", content="先保持红色"),
                    ScenarioStep("memory_save"),
                    ScenarioStep("restart_session"),
                ),
                runtime_factory,
            )
            resources.callback(observation.runtime.close)

            memory = observation.runtime.current.context.conversation_memory
            self.assertEqual(2, observation.runtime_instances)
            self.assertEqual("session-main", observation.runtime.current.record.id)
            self.assertEqual(2, len(observation.runtime.store.list_all()))
            self.assertEqual("保持红色约束", memory.constraints[0].text)
            self.assertEqual("strict", observation.runtime.current.memory.permission_level)
            self.assertNotIn(
                observation.runtime.current.context.verification,
                {"passed", "通过"},
            )

    def test_named_scenario_target_rejects_duplicates_but_full_id_is_exact(self) -> None:
        """名称只作便利目标；同名时必须报歧义，完整 ID 始终稳定。"""

        first = SimpleNamespace(id="id-first", name="duplicate")
        second = SimpleNamespace(id="id-second", name="duplicate")

        ambiguous = mock.Mock()
        ambiguous.store.list_all.return_value = [first, second]
        ambiguous.close.return_value = True
        with self.assertRaisesRegex(ValueError, "歧义"):
            run_runtime_scenario(
                (ScenarioStep("switch_session", target="duplicate"),),
                lambda _initial=None: ambiguous,
            )
        ambiguous.switch.assert_not_called()

        exact = mock.Mock()
        exact.store.list_all.return_value = [first, second]
        exact.close.return_value = True
        observation = run_runtime_scenario(
            (ScenarioStep("switch_session", target=second.id),),
            lambda _initial=None: exact,
        )
        self.addCleanup(observation.runtime.close)
        exact.switch.assert_called_once_with(
            second.id,
            confirm=mock.ANY,
        )

    def test_scenario_exception_reports_incomplete_runtime_cleanup(self) -> None:
        """步骤异常后的 close=False 必须升级为生命周期失败，不能假装已释放资源。"""

        runtime = mock.Mock()
        runtime.run_task.side_effect = ValueError("synthetic step failure")
        runtime.close.return_value = False

        with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
            run_runtime_scenario(
                (ScenarioStep("user_turn", content="trigger"),),
                lambda _initial=None: runtime,
            )

        runtime.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
