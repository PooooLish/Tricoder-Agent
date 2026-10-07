"""需求澄清工具、批次边界与失败历史的回归测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tricoder.agent import CodingAgent
from tricoder.context.manager import ContextBudget, ContextManager
from tricoder.context.memory import ConversationMemory, with_task_termination_facts
from tricoder.core.cancellation import CancellationToken
from tricoder.core.clarification import (
    ClarificationRequest,
    ClarificationResult,
    ClarificationStatus,
)
from tricoder.execution_state import EffectState, ErrorCode
from tricoder.models import ProviderResponse, SessionContext, ToolCall
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.protocols import NativeToolProtocol
from tricoder.session.runtime import RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.session.store import SessionStore
from tricoder.tools import ToolContext, ToolRegistry


class QueueProvider:
    """只返回预置响应，并保留模型实际看到的请求历史。"""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.histories: list[list[object]] = []

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        self.histories.append(list(messages))
        if not self.responses:
            raise AssertionError("Provider 不应收到额外请求")
        return self.responses.pop(0)


def tool_response(*calls: ToolCall) -> ProviderResponse:
    return ProviderResponse(tool_calls=tuple(calls), finish_reason="tool_calls")


class ClarificationContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def registry(self, clarifier=None, *, approver=None) -> ToolRegistry:  # type: ignore[no-untyped-def]
        return ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.root),
                CommandPolicy(self.root),
                approver or (lambda *_: False),
                clarifier=clarifier,
                clarification_timeout=300.0,
            )
        )

    def test_request_and_result_contracts_are_bounded(self) -> None:
        request = ClarificationRequest("host-id", "选择实现方式？", ("A", "B"))
        self.assertEqual(("A", "B"), request.options)
        self.assertEqual(
            "自由文本",
            ClarificationResult.answered("自由文本").answer,
        )
        with self.assertRaises(ValueError):
            ClarificationRequest("id", "问题", ("重复", "重复"))
        with self.assertRaises(ValueError):
            ClarificationRequest("id", "", ())
        with self.assertRaises(ValueError):
            ClarificationResult(ClarificationStatus.TIMED_OUT, "不允许")
        with self.assertRaises(ValueError):
            ClarificationResult.answered(" ")
        with self.assertRaises(ValueError):
            ClarificationResult.answered("x" * 4001)

    async def test_answered_tool_uses_host_id_and_never_requests_approval(self) -> None:
        requests: list[tuple[ClarificationRequest, float]] = []
        approvals: list[tuple[str, str]] = []

        def clarify(request, cancellation, timeout):  # type: ignore[no-untyped-def]
            self.assertFalse(cancellation.is_cancelled)
            requests.append((request, timeout))
            return ClarificationResult.answered("使用 SQLite")

        result = await self.registry(
            clarify,
            approver=lambda action, detail: approvals.append((action, detail)) or True,
        ).execute_async(
            "ask_user",
            {"question": "存储用什么？", "options": ["SQLite", "JSON"]},
            cancellation=CancellationToken(),
            call_id="model-call-id",
        )

        self.assertTrue(result.ok)
        self.assertEqual([], approvals)
        self.assertEqual(1, len(requests))
        self.assertNotEqual("model-call-id", requests[0][0].request_id)
        self.assertEqual(300.0, requests[0][1])
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertEqual(ClarificationStatus.ANSWERED, result.clarification.status)
        self.assertEqual("使用 SQLite", result.clarification.answer)
        payload = json.loads(result.output)
        self.assertEqual("answered", payload["status"])
        self.assertEqual("使用 SQLite", payload["answer"])

    async def test_invalid_options_do_not_invoke_host(self) -> None:
        called = False

        def clarify(*_args):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return ClarificationResult.answered("x")

        result = await self.registry(clarify).execute_async(
            "ask_user",
            {"question": "选择？", "options": ["same", "same"]},
            cancellation=CancellationToken(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
        self.assertFalse(called)

    async def test_missing_host_is_typed_needs_input(self) -> None:
        result = await self.registry().execute_async(
            "ask_user",
            {"question": "需要用户信息"},
            cancellation=CancellationToken(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.NEEDS_INPUT, result.error.code)
        self.assertEqual(ClarificationStatus.UNAVAILABLE, result.clarification.status)

    async def test_external_edit_discards_answer_and_stops(self) -> None:
        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            (self.root / "outside.txt").write_text("changed", encoding="utf-8")
            return ClarificationResult.answered("秘密答案不应被采用")

        result = await self.registry(clarify).execute_async(
            "ask_user",
            {"question": "继续吗？"},
            cancellation=CancellationToken(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.NEEDS_INPUT, result.error.code)
        self.assertEqual(ClarificationStatus.UNAVAILABLE, result.clarification.status)
        self.assertEqual("workspace_changed", result.clarification.reason)
        self.assertNotIn("秘密答案", result.output)

    async def test_callback_failure_is_unavailable_not_invalid_argument(self) -> None:
        def clarify(*_args):  # type: ignore[no-untyped-def]
            raise RuntimeError("synthetic host failure")

        result = await self.registry(clarify).execute_async(
            "ask_user",
            {"question": "需要回答"},
            cancellation=CancellationToken(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.NEEDS_INPUT, result.error.code)
        self.assertEqual("host_failed", result.clarification.reason)

    async def test_workspace_scan_failure_does_not_invoke_callback(self) -> None:
        called = False

        def clarify(*_args):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return ClarificationResult.answered("x")

        registry = self.registry(clarify)
        with mock.patch.object(
            type(registry.context.verification_scope),
            "capture",
            side_effect=OSError("synthetic scan failure"),
        ):
            result = await registry.execute_async(
                "ask_user",
                {"question": "需要回答"},
                cancellation=CancellationToken(),
            )
        self.assertFalse(result.ok)
        self.assertEqual("workspace_scan_failed", result.clarification.reason)
        self.assertFalse(called)

    async def test_cancellation_discards_simultaneous_late_answer(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            entered.set()
            release.wait(2)
            return ClarificationResult.answered("late answer")

        registry = self.registry(clarify)
        token = CancellationToken()
        task = asyncio.create_task(
            registry.execute_async(
                "ask_user",
                {"question": "等待取消"},
                cancellation=token,
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        token.cancel()
        release.set()
        result = await asyncio.wait_for(task, 2)
        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.CANCELLED, result.error.code)
        self.assertEqual(ClarificationStatus.CANCELLED, result.clarification.status)
        self.assertNotIn("late answer", result.output)

    async def test_sync_registry_wait_uses_callers_cancellation_token(self) -> None:
        entered = threading.Event()

        def clarify(_request, cancellation, _timeout):  # type: ignore[no-untyped-def]
            entered.set()
            cancellation.wait(1)
            if cancellation.is_cancelled:
                return ClarificationResult.cancelled()
            return ClarificationResult.timed_out()

        registry = self.registry(clarify)
        token = CancellationToken()
        task = asyncio.create_task(
            asyncio.to_thread(
                registry.execute,
                "ask_user",
                {"question": "等待同步入口取消"},
                cancellation=token,
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        token.cancel()

        result = await asyncio.wait_for(task, 1)

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.CANCELLED, result.error.code)
        self.assertEqual(ClarificationStatus.CANCELLED, result.clarification.status)


class ClarificationAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_agent(self, responses, clarifier, *, approvals=None):  # type: ignore[no-untyped-def]
        approvals = approvals if approvals is not None else []
        tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.root),
                CommandPolicy(self.root),
                lambda action, detail: approvals.append((action, detail)) or True,
                clarifier=clarifier,
            )
        )
        provider = QueueProvider(list(responses))
        return CodingAgent(provider, tools, max_rounds=10, plan_enabled=False), provider, approvals

    def test_answer_skips_same_batch_write_and_reprompts_model(self) -> None:
        answer_calls = 0

        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            nonlocal answer_calls
            answer_calls += 1
            return ClarificationResult.answered("选择 A")

        first_calls = (
            ToolCall("ask-1", "ask_user", {"question": "A 还是 B？", "options": ["A", "B"]}),
            ToolCall("write-1", "create_file", {"path": "should-not-exist.txt", "content": "bad"}),
        )
        agent, provider, approvals = self.make_agent(
            [
                tool_response(*first_calls),
                tool_response(ToolCall("finish-1", "finish", {"summary": "已根据回答结束"})),
            ],
            clarify,
        )

        turn = agent.run_with_context("先问清楚", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual(1, answer_calls)
        self.assertFalse((self.root / "should-not-exist.txt").exists())
        self.assertEqual([], approvals)
        self.assertEqual(2, len(provider.histories))
        second = provider.histories[1]
        outputs = [json.loads(message.content)["tool_result"] for message in second if message.role == "tool"]
        self.assertEqual("answered", json.loads(outputs[0]["output"])["status"])
        self.assertEqual("skipped", outputs[1]["error"]["code"])

    def test_third_valid_question_stops_without_invoking_host_again(self) -> None:
        answers: list[str] = []

        def clarify(request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            answers.append(request.question)
            return ClarificationResult.answered(f"answer-{len(answers)}")

        agent, provider, _approvals = self.make_agent(
            [
                tool_response(ToolCall("ask-1", "ask_user", {"question": "问题一"})),
                tool_response(ToolCall("ask-2", "ask_user", {"question": "问题二"})),
                tool_response(ToolCall("ask-3", "ask_user", {"question": "问题三"})),
                tool_response(ToolCall("unexpected", "finish", {"summary": "不应执行"})),
            ],
            clarify,
        )

        turn = agent.run_with_context("需要多次澄清", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual(["问题一", "问题二"], answers)
        self.assertEqual(3, len(provider.histories))
        self.assertIn("提问次数", turn.result.summary)
        self.assertEqual("task_termination", turn.context.messages[-1].kind)
        self.assertGreater(turn.context.latest_completed_task_seq, 0)
        plan = ContextManager(
            ContextBudget(max_chars=100_000), NativeToolProtocol()
        ).plan_save_candidate(turn.context.messages, covered_through=0)
        self.assertTrue(plan.needs_summary, plan.reason)

    def test_timeout_closes_failed_history_for_later_task(self) -> None:
        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            return ClarificationResult.timed_out()

        agent, provider, _approvals = self.make_agent(
            [
                tool_response(ToolCall("ask-timeout", "ask_user", {"question": "请回答"})),
                tool_response(ToolCall("finish-next", "finish", {"summary": "后续任务完成"})),
            ],
            clarify,
        )

        failed = agent.run_with_context("等待回答", SessionContext())
        succeeded = agent.run_with_context("后续任务", failed.context)

        self.assertFalse(failed.result.ok)
        self.assertTrue(succeeded.result.ok, succeeded.result.summary)
        self.assertGreater(failed.context.latest_completed_task_seq, 0)
        self.assertGreater(succeeded.context.latest_completed_task_seq, 0)
        old_terminal = next(
            message for message in failed.context.messages if message.kind == "task_termination"
        )
        self.assertIn("等待用户回答超时", old_terminal.content)
        save = ContextManager(
            ContextBudget(max_chars=100_000), NativeToolProtocol()
        ).plan_save_candidate(succeeded.context.messages, covered_through=0)
        self.assertTrue(save.needs_summary, save.reason)
        candidate = with_task_termination_facts(
            ConversationMemory(), save.source_messages
        )
        self.assertEqual(1, len(candidate.open_items))
        self.assertEqual("pending", candidate.open_items[0].state)
        self.assertIn("回答超时", candidate.open_items[0].text)

    def test_answer_does_not_approve_later_write(self) -> None:
        approvals: list[tuple[str, str]] = []

        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            return ClarificationResult.answered("请创建文件")

        tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.root),
                CommandPolicy(self.root),
                lambda action, detail: approvals.append((action, detail)) or False,
                clarifier=clarify,
            )
        )
        provider = QueueProvider(
            [
                tool_response(ToolCall("ask", "ask_user", {"question": "是否创建？"})),
                tool_response(
                    ToolCall(
                        "write",
                        "create_file",
                        {"path": "not-approved.txt", "content": "x"},
                    )
                ),
            ]
        )
        result = CodingAgent(
            provider, tools, max_rounds=3, plan_enabled=False
        ).run("问后写入")
        self.assertFalse(result.ok)
        self.assertEqual(1, len(approvals))
        self.assertFalse((self.root / "not-approved.txt").exists())

    def test_cancelled_question_is_paired_and_closed_without_success(self) -> None:
        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            return ClarificationResult.cancelled()

        call = ToolCall("ask-cancel", "ask_user", {"question": "等待取消"})
        agent, _provider, _approvals = self.make_agent(
            [tool_response(call)], clarify
        )
        turn = agent.run_with_context("取消澄清", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertEqual("任务已取消", turn.result.summary)
        self.assertGreater(turn.context.latest_completed_task_seq, 0)
        tool_results = [
            message
            for message in turn.context.messages
            if message.role == "tool" and message.tool_call_id == call.id
        ]
        self.assertEqual(1, len(tool_results))
        payload = json.loads(tool_results[0].content)["tool_result"]
        self.assertEqual("cancelled", payload["error"]["code"])
        self.assertEqual("task_termination", turn.context.messages[-1].kind)
        save = ContextManager(
            ContextBudget(max_chars=100_000), NativeToolProtocol()
        ).plan_save_candidate(turn.context.messages, covered_through=0)
        self.assertTrue(save.needs_summary, save.reason)

    def test_two_agents_keep_answers_and_question_budgets_isolated(self) -> None:
        barrier = threading.Barrier(2)

        def make(label: str):
            root = self.root / label
            root.mkdir()

            def clarify(request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
                barrier.wait(2)
                return ClarificationResult.answered(f"{label}:{request.question}")

            provider = QueueProvider(
                [
                    tool_response(
                        ToolCall(f"ask-{label}", "ask_user", {"question": label})
                    ),
                    tool_response(
                        ToolCall(f"finish-{label}", "finish", {"summary": label})
                    ),
                ]
            )
            registry = ToolRegistry(
                ToolContext(
                    WorkspacePolicy(root),
                    CommandPolicy(root),
                    lambda *_: False,
                    clarifier=clarify,
                )
            )
            return CodingAgent(provider, registry, max_rounds=3, plan_enabled=False), provider

        left, left_provider = make("left")
        right, right_provider = make("right")
        results: list[object] = []
        failures: list[BaseException] = []

        def run(agent, task):  # type: ignore[no-untyped-def]
            try:
                results.append(agent.run(task))
            except BaseException as exc:
                failures.append(exc)

        workers = [
            threading.Thread(target=run, args=(left, "left")),
            threading.Thread(target=run, args=(right, "right")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual([], failures)
        self.assertTrue(all(getattr(result, "ok", False) for result in results))
        left_text = "\n".join(
            message.content or "" for message in left_provider.histories[1]
        )
        right_text = "\n".join(
            message.content or "" for message in right_provider.histories[1]
        )
        self.assertIn("left:left", left_text)
        self.assertNotIn("right:right", left_text)
        self.assertIn("right:right", right_text)
        self.assertNotIn("left:left", right_text)


class ClarificationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def runtime(
        self,
        name: str,
        provider: QueueProvider,
        clarifier,
        *,
        confirmer=lambda _preview: True,
    ) -> SessionRuntime:  # type: ignore[no-untyped-def]
        store = SessionStore(self.root / f"{name}.db")
        runtime = SessionRuntime(
            store,
            self.workspace,
            options=RuntimeOptions(
                environ={"OPENAI_API_KEY": "synthetic-key"},
                audit_dir=self.root / f"audit-{name}",
                plan_enabled=False,
                max_rounds=5,
            ),
            provider_factory=lambda _config, _timeout: provider,
            clarifier=clarifier,
            workspace_confirmer=confirmer,
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_wait_keeps_session_task_and_workspace_locks(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def clarify(_request, cancellation, timeout):  # type: ignore[no-untyped-def]
            self.assertEqual(300.0, timeout)
            entered.set()
            while not release.wait(0.02):
                if cancellation.is_cancelled:
                    return ClarificationResult.cancelled()
            return ClarificationResult.answered("continue")

        provider = QueueProvider(
            [
                tool_response(ToolCall("ask", "ask_user", {"question": "继续？"})),
                tool_response(ToolCall("finish", "finish", {"summary": "done"})),
            ]
        )
        runtime = self.runtime("primary", provider, clarify)
        secondary_provider = QueueProvider(
            [tool_response(ToolCall("unexpected", "finish", {"summary": "bad"}))]
        )
        secondary = self.runtime(
            "secondary",
            secondary_provider,
            lambda *_: ClarificationResult.unavailable("noninteractive"),
        )
        results: list[object] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                results.append(runtime.run_task("需要澄清"))
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(5))
        self.assertFalse(runtime._task_lock.acquire(blocking=False))
        with self.assertRaisesRegex(SessionRuntimeError, "工作区"):
            secondary.run_task("竞争同一工作区")
        self.assertEqual(0, len(secondary_provider.histories))
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].ok)

    def test_external_edit_during_wait_requires_next_task_gate_confirmation(self) -> None:
        previews = []

        def clarify(_request, _cancellation, _timeout):  # type: ignore[no-untyped-def]
            (self.workspace / "external.txt").write_text("outside", encoding="utf-8")
            return ClarificationResult.answered("answer must be discarded")

        def confirm(preview):  # type: ignore[no-untyped-def]
            previews.append(preview)
            return False

        provider = QueueProvider(
            [tool_response(ToolCall("ask", "ask_user", {"question": "等待外部编辑"}))]
        )
        runtime = self.runtime("external", provider, clarify, confirmer=confirm)

        first = runtime.run_task("第一项任务")

        self.assertFalse(first.ok)
        self.assertIn("工作区", first.summary)
        self.assertEqual(1, len(provider.histories))
        with self.assertRaisesRegex(SessionRuntimeError, "工作区变化"):
            runtime.run_task("下一项任务")
        self.assertEqual(1, len(provider.histories))
        self.assertEqual(1, len(previews))
        self.assertIn("external.txt", previews[0].changed_paths)


if __name__ == "__main__":
    unittest.main()
