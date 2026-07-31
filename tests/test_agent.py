import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder import tools as tools_module
from tricoder.agent import (
    CONTEXT_COMPACTION_NOTICE,
    LEGACY_SYSTEM_PROMPT,
    CodingAgent,
    compact_messages,
    parse_action,
)
from tricoder.audit import AuditLogger
from tricoder.models import (
    Message,
    ProviderResponse,
    RunResult,
    SessionContext,
    ToolCall,
    ToolDefinition,
)
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.providers import ProviderError, ProviderProtocolError
from tricoder.tools import ToolContext, ToolRegistry


class CloseFailingBinding:
    """让真实目录绑定完成关闭后报告 OSError。"""

    def __init__(self, binding: object) -> None:
        self.binding = binding

    def __getattr__(self, name: str) -> object:
        return getattr(self.binding, name)

    def close(self) -> None:
        self.binding.close()  # type: ignore[attr-defined]
        raise OSError("simulated binding close failure")


def patch_binding_close_failure() -> object:
    real_open = tools_module._DirectoryBinding.open

    def open_with_failing_close(workspace: Path, parent: Path) -> CloseFailingBinding:
        return CloseFailingBinding(real_open(workspace, parent))

    return patch.object(
        tools_module._DirectoryBinding,
        "open",
        side_effect=open_with_failing_close,
    )


class ScriptedProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.histories: list[list[Message]] = []
        self.tool_batches: list[tuple[ToolDefinition, ...]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        self.tool_batches.append(tuple(tools))
        return ProviderResponse(content=self.responses.pop(0))


class StructuredScriptedProvider:
    """记录原生工具定义和结构化消息历史的确定性 Provider。"""

    def __init__(
        self,
        responses: list[ProviderResponse | ProviderError],
    ) -> None:
        self.responses = list(responses)
        self.histories: list[list[Message]] = []
        self.tool_batches: list[tuple[ToolDefinition, ...]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        self.tool_batches.append(tuple(tools))
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


class PublicToolRegistry:
    """只暴露 Agent 所需公开接口，防止测试放任私有注册表耦合。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def context(self) -> ToolContext:
        return self._registry.context

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions

    def contains(self, name: str) -> bool:
        return self._registry.contains(name)

    def describe(self, name: str) -> ToolDefinition | None:
        return self._registry.describe(name)

    def execute(self, name: str, arguments: dict[str, object]) -> object:
        return self._registry.execute(name, arguments)


class FailingProvider:
    """记录请求后模拟可恢复的 Provider 错误。"""

    def __init__(self) -> None:
        self.histories: list[list[Message]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        raise ProviderError("模拟 Provider 故障")


class FailingOnSecondRequestProvider:
    """让首轮工具成功、第二次模型请求失败。"""

    def __init__(self, first_response: str) -> None:
        self.first_response = first_response
        self.histories: list[list[Message]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        if len(self.histories) == 2:
            raise ProviderError("模拟第二次请求故障")
        return ProviderResponse(content=self.first_response)


class FailingAudit:
    """模拟工具执行后才发生的审计写入失败。"""

    def prepare(self) -> None:
        return None

    def log(self, event: dict[str, object]) -> None:
        raise OSError("模拟审计故障")


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_round_start(self, round_number: int, max_rounds: int) -> None:
        self.events.append(f"round:{round_number}/{max_rounds}")

    def on_action(self, current_action: object) -> None:
        self.events.append(f"action:{current_action.tool}")  # type: ignore[attr-defined]

    def on_tool_result(
        self,
        current_action: object,
        result: object,
        duration_ms: int,
    ) -> None:
        self.events.append(
            f"result:{current_action.tool}:{result.ok}"  # type: ignore[attr-defined]
        )

    def on_error(self, message: str) -> None:
        self.events.append(f"error:{message}")


class CapturingObserver(RecordingObserver):
    """保留公开动作，便于断言 reason 不来自模型自由文本。"""

    def __init__(self) -> None:
        super().__init__()
        self.actions: list[object] = []

    def on_action(self, current_action: object) -> None:
        self.actions.append(current_action)
        super().on_action(current_action)


def action(tool: str, arguments: dict[str, object], reason: str = "测试") -> str:
    return json.dumps(
        {"tool": tool, "arguments": arguments, "reason": reason},
        ensure_ascii=False,
    )


class NativeToolCallingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approver=lambda _action, _detail: True,
                timeout=5,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def response(call_id: str, name: str, arguments: dict[str, object]) -> ProviderResponse:
        return ProviderResponse(
            tool_calls=(ToolCall(call_id, name, arguments),),
            finish_reason="tool_calls",
        )

    def test_default_native_mode_passes_definitions_and_preserves_tool_pair(self) -> None:
        """工具定义、调用 ID 或结构化历史任一丢失时都应失败。"""
        provider = StructuredScriptedProvider(
            [
                self.response("call-read", "read_file", {"path": "sample.py"}),
                self.response("call-finish", "finish", {"summary": "读取完成"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=2).run("读取示例")

        self.assertTrue(result.ok)
        self.assertEqual(self.tools.definitions, provider.tool_batches[0])
        self.assertEqual(self.tools.definitions, provider.tool_batches[1])
        assistant, tool = provider.histories[1][-2:]
        self.assertEqual("assistant", assistant.role)
        self.assertEqual(("call-read",), tuple(call.id for call in assistant.tool_calls))
        self.assertEqual("tool", tool.role)
        self.assertEqual("call-read", tool.tool_call_id)
        self.assertIn('"tool": "read_file"', tool.content or "")

    def test_plain_text_gets_controlled_feedback_without_parsing_content_json(self) -> None:
        """原生模式不得把普通文本中的 JSON 当作工具调用执行。"""
        provider = StructuredScriptedProvider(
            [
                ProviderResponse(
                    content=action("create_file", {"path": "leak.py", "content": "bad"})
                ),
                self.response("call-finish", "finish", {"summary": "已改用工具调用"}),
            ]
        )

        with patch("tricoder.agent.parse_action", side_effect=AssertionError("不应解析")):
            result = CodingAgent(provider, self.tools, max_rounds=2).run("检查协议")

        self.assertTrue(result.ok)
        self.assertFalse((self.workspace / "leak.py").exists())
        feedback = provider.histories[1][-1]
        self.assertEqual("user", feedback.role)
        self.assertIn("工具调用", feedback.content or "")
        self.assertNotIn("leak.py", feedback.content or "")

    def test_multiple_tool_calls_execute_none_and_request_one_call(self) -> None:
        """并行调用不得产生部分执行或文件副作用。"""
        provider = StructuredScriptedProvider(
            [
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "call-create",
                            "create_file",
                            {"path": "created.py", "content": "created = True\n"},
                        ),
                        ToolCall(
                            "call-edit",
                            "edit_file",
                            {
                                "path": "sample.py",
                                "old_text": "value = 1",
                                "new_text": "value = 2",
                            },
                        ),
                    )
                ),
                self.response("call-finish", "finish", {"summary": "已选择单个调用"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=2).run("拒绝并行调用")

        self.assertTrue(result.ok)
        self.assertFalse((self.workspace / "created.py").exists())
        self.assertEqual(
            "value = 1\n",
            (self.workspace / "sample.py").read_text(encoding="utf-8"),
        )
        feedback = provider.histories[1][-1]
        self.assertEqual("user", feedback.role)
        self.assertIn("一个", feedback.content or "")

    def test_protocol_error_can_recover_but_provider_error_stops_and_rolls_back(
        self,
    ) -> None:
        """只有协议转换错误可进入受控纠错，传输类错误仍立即停止。"""
        protocol_sentinel = "PROTOCOL-SENTINEL-PRIVATE"
        recovering = StructuredScriptedProvider(
            [
                ProviderProtocolError(protocol_sentinel),
                self.response("call-finish", "finish", {"summary": "协议已修正"}),
            ]
        )

        recovered = CodingAgent(recovering, self.tools, max_rounds=2).run("修正协议")

        self.assertTrue(recovered.ok)
        feedback = recovering.histories[1][-1]
        self.assertIn("协议", feedback.content or "")
        self.assertNotIn(protocol_sentinel, feedback.content or "")

        failing = StructuredScriptedProvider([ProviderError("HTTP 401")])
        failed = CodingAgent(failing, self.tools, max_rounds=2).run_with_context(
            "不可恢复",
            SessionContext(),
        )

        self.assertFalse(failed.result.ok)
        self.assertEqual(1, len(failing.histories))
        self.assertEqual((), failed.context.messages)

    def test_native_correction_audit_failure_does_not_commit_orphan_feedback(
        self,
    ) -> None:
        """纠错审计失败必须回滚尚未形成完整工具回合的当前任务。"""
        cases = {
            "plain_text": ProviderResponse(content="普通文本"),
            "multiple_calls": ProviderResponse(
                tool_calls=(
                    ToolCall("call-one", "read_file", {"path": "sample.py"}),
                    ToolCall("call-two", "finish", {"summary": "完成"}),
                )
            ),
            "protocol_error": ProviderProtocolError("协议哨兵"),
        }

        for name, response in cases.items():
            with self.subTest(name=name):
                provider = StructuredScriptedProvider([response])
                failed = CodingAgent(
                    provider,
                    self.tools,
                    max_rounds=1,
                    audit=FailingAudit(),
                ).run_with_context("触发纠错审计失败", SessionContext())

                self.assertFalse(failed.result.ok)
                self.assertEqual((), failed.context.messages)

    def test_unknown_tool_returns_complete_assistant_tool_pair(self) -> None:
        """未知名称也必须用相同调用 ID 返回工具错误，不能破坏消息协议。"""
        provider = StructuredScriptedProvider(
            [
                self.response("call-unknown", "delete_everything", {"secret": "value"}),
                self.response("call-finish", "finish", {"summary": "已改用安全工具"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=2).run("处理未知工具")

        self.assertTrue(result.ok)
        assistant, tool = provider.histories[1][-2:]
        self.assertEqual("call-unknown", assistant.tool_calls[0].id)
        self.assertEqual("tool", tool.role)
        self.assertEqual("call-unknown", tool.tool_call_id)
        self.assertIn("未知工具", tool.content or "")

    def test_native_session_reuses_complete_assistant_tool_history(self) -> None:
        """跨任务压缩不得丢弃原生 assistant/tool 完整回合。"""
        first_provider = StructuredScriptedProvider(
            [
                self.response("call-read", "read_file", {"path": "sample.py"}),
                self.response("call-finish", "finish", {"summary": "首轮完成"}),
            ]
        )
        first = CodingAgent(first_provider, self.tools, max_rounds=2).run_with_context(
            "读取示例",
            SessionContext(),
        )
        second_provider = StructuredScriptedProvider(
            [self.response("call-finish-2", "finish", {"summary": "继续完成"})]
        )

        second = CodingAgent(
            second_provider,
            self.tools,
            max_rounds=1,
        ).run_with_context("继续检查", first.context)

        self.assertTrue(second.result.ok)
        reused = second_provider.histories[0]
        self.assertTrue(
            any(
                message.role == "assistant"
                and message.tool_calls
                and message.tool_calls[0].id == "call-read"
                for message in reused
            )
        )
        self.assertTrue(
            any(
                message.role == "tool" and message.tool_call_id == "call-read"
                for message in reused
            )
        )

    def test_native_history_drops_correction_noise_but_reuses_later_tool_rounds(
        self,
    ) -> None:
        """旧任务归一化只淘汰纠错消息，不得连带丢失之后的成功回合。"""
        corrections = {
            "plain_text": ProviderResponse(content="普通文本"),
            "multiple_calls": ProviderResponse(
                tool_calls=(
                    ToolCall("call-one", "read_file", {"path": "sample.py"}),
                    ToolCall("call-two", "finish", {"summary": "错误并行"}),
                )
            ),
            "protocol_error": ProviderProtocolError("协议哨兵"),
        }

        for name, correction in corrections.items():
            with self.subTest(name=name):
                first_provider = StructuredScriptedProvider(
                    [
                        correction,
                        self.response(
                            f"call-read-{name}",
                            "read_file",
                            {"path": "sample.py"},
                        ),
                        self.response(
                            f"call-finish-{name}",
                            "finish",
                            {"summary": "已修正"},
                        ),
                    ]
                )
                first = CodingAgent(
                    first_provider,
                    self.tools,
                    max_rounds=3,
                ).run_with_context("先纠错再完成", SessionContext())
                second_provider = StructuredScriptedProvider(
                    [
                        self.response(
                            f"call-next-{name}",
                            "finish",
                            {"summary": "下一任务完成"},
                        )
                    ]
                )

                CodingAgent(
                    second_provider,
                    self.tools,
                    max_rounds=1,
                ).run_with_context("继续任务", first.context)

                reused = second_provider.histories[0]
                self.assertTrue(
                    any(
                        message.role == "tool"
                        and message.tool_call_id == f"call-read-{name}"
                        for message in reused
                    )
                )
                self.assertFalse(
                    any(message.kind == "protocol_feedback" for message in reused)
                )
                self.assertFalse(
                    any(message.content == "普通文本" for message in reused)
                )

    def test_compaction_drops_native_round_with_mismatched_call_id(self) -> None:
        """assistant 与 tool 的调用 ID 不一致时不得作为完整回合保留。"""
        fixed = [Message("system", "规则"), Message("user", "任务")]
        invalid = [
            Message(
                "assistant",
                None,
                tool_calls=(ToolCall("call-a", "read_file", {"path": "sample.py"}),),
            ),
            Message(
                "tool",
                '{"ok": false}',
                kind="tool_result",
                tool_call_id="call-b",
            ),
        ]

        compacted = compact_messages([*fixed, *invalid], 10_000)

        self.assertEqual(
            [*fixed, Message("system", CONTEXT_COMPACTION_NOTICE)],
            compacted,
        )

    def test_compaction_drops_native_round_with_multiple_assistant_calls(self) -> None:
        """assistant 携带多个调用时不能用其中一个 tool 结果伪装完整回合。"""
        fixed = [Message("system", "规则"), Message("user", "任务")]
        invalid = [
            Message(
                "assistant",
                None,
                tool_calls=(
                    ToolCall("call-a", "read_file", {"path": "sample.py"}),
                    ToolCall("call-b", "finish", {"summary": "完成"}),
                ),
            ),
            Message(
                "tool",
                '{"ok": true}',
                kind="tool_result",
                tool_call_id="call-a",
            ),
        ]

        compacted = compact_messages([*fixed, *invalid], 10_000)

        self.assertEqual(
            [*fixed, Message("system", CONTEXT_COMPACTION_NOTICE)],
            compacted,
        )

    def test_native_mode_drops_legacy_history_rounds(self) -> None:
        """原生请求不得混入旧版 assistant/user 工具回合。"""
        context = SessionContext(
            messages=(
                Message("user", "旧版任务", kind="task"),
                Message("assistant", action("read_file", {"path": "sample.py"})),
                Message(
                    "user",
                    '{"tool_result":{"ok":true}}',
                    kind="tool_result",
                ),
            )
        )
        provider = StructuredScriptedProvider(
            [self.response("call-finish", "finish", {"summary": "完成"})]
        )

        CodingAgent(provider, self.tools, max_rounds=1).run_with_context(
            "原生任务",
            context,
        )

        request = provider.histories[0]
        self.assertFalse(any(message.content == "旧版任务" for message in request))
        self.assertFalse(
            any(
                message.content == action("read_file", {"path": "sample.py"})
                for message in request
            )
        )

    def test_finish_keeps_write_verification_requirement(self) -> None:
        """原生 finish 不得绕过现有的写后验证判定。"""
        provider = StructuredScriptedProvider(
            [
                self.response(
                    "call-edit",
                    "edit_file",
                    {
                        "path": "sample.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                    },
                ),
                self.response("call-finish", "finish", {"summary": "修改完成"}),
            ]
        )

        result = CodingAgent(provider, self.tools, max_rounds=2).run("修改后结束")

        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)
        self.assertIn("尚未运行验证命令", result.summary)

    def test_observer_reason_and_audit_use_only_public_static_metadata(self) -> None:
        """Agent 不得读取私有注册表，也不得审计模型传入的完整参数。"""
        content_sentinel = "ARGUMENT-CONTENT-SENTINEL-PRIVATE"
        provider = StructuredScriptedProvider(
            [
                self.response(
                    "call-create",
                    "create_file",
                    {"path": "created.py", "content": content_sentinel},
                ),
                self.response(
                    "call-verify",
                    "run_command",
                    {"command": "python -m compileall -q created.py"},
                ),
                self.response("call-finish", "finish", {"summary": "完成"}),
            ]
        )
        observer = CapturingObserver()
        audit_path = self.workspace / "runtime" / "native.jsonl"
        public_tools = PublicToolRegistry(self.tools)
        agent = CodingAgent(
            provider,
            public_tools,  # type: ignore[arg-type]
            max_rounds=3,
            audit=AuditLogger(audit_path),
            observer=observer,
        )

        result = agent.run("创建并验证文件")
        trail = audit_path.read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        definition = self.tools.describe("create_file")
        self.assertIsNotNone(definition)
        self.assertEqual(
            definition.description,  # type: ignore[union-attr]
            observer.actions[0].reason,  # type: ignore[attr-defined]
        )
        self.assertNotIn(content_sentinel, trail)


class LegacyJsonModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approver=lambda _action, _detail: True,
                timeout=5,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_explicit_legacy_mode_passes_no_tools_and_parses_only_content(self) -> None:
        """旧版只能消费 content，不能执行响应中附带的原生 ToolCall。"""
        provider = StructuredScriptedProvider(
            [
                ProviderResponse(
                    content=action("finish", {"summary": "旧版完成"}),
                    tool_calls=(
                        ToolCall(
                            "call-create",
                            "create_file",
                            {"path": "unexpected.py", "content": "bad = True\n"},
                        ),
                    ),
                )
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=1,
            tool_protocol="legacy_json",
        ).run("兼容旧版")

        self.assertTrue(result.ok)
        self.assertEqual([()], provider.tool_batches)
        self.assertFalse((self.workspace / "unexpected.py").exists())

    def test_legacy_invalid_json_is_returned_for_self_correction(self) -> None:
        """显式旧版路径必须保留非法 JSON 的可恢复反馈。"""
        provider = StructuredScriptedProvider(
            [
                ProviderResponse(content="这不是 JSON"),
                ProviderResponse(content=action("finish", {"summary": "格式已修正"})),
            ]
        )

        result = CodingAgent(
            provider,
            self.tools,
            max_rounds=2,
            tool_protocol="legacy_json",
        ).run("修正旧版格式")

        self.assertTrue(result.ok)
        self.assertTrue(
            any(
                message.kind == "tool_result" and "动作格式错误" in (message.content or "")
                for message in provider.histories[1]
            )
        )

    def test_legacy_mode_drops_native_history_rounds(self) -> None:
        """旧版请求不得混入原生 assistant/tool 工具回合。"""
        context = SessionContext(
            messages=(
                Message("user", "原生任务", kind="task"),
                Message(
                    "assistant",
                    None,
                    tool_calls=(
                        ToolCall("call-native", "read_file", {"path": "sample.py"}),
                    ),
                ),
                Message(
                    "tool",
                    '{"tool_result":{"ok":true}}',
                    kind="tool_result",
                    tool_call_id="call-native",
                ),
            )
        )
        provider = StructuredScriptedProvider(
            [ProviderResponse(content=action("finish", {"summary": "完成"}))]
        )

        CodingAgent(
            provider,
            self.tools,
            max_rounds=1,
            tool_protocol="legacy_json",
        ).run_with_context("旧版任务", context)

        request = provider.histories[0]
        self.assertFalse(any(message.content == "原生任务" for message in request))
        self.assertFalse(
            any(message.tool_call_id == "call-native" for message in request)
        )


class LegacyCodingAgent(CodingAgent):
    """让既有测试显式锁定旧版 JSON 协议。"""

    def __init__(self, provider: object, tools: ToolRegistry, **kwargs: object) -> None:
        super().__init__(
            provider,  # type: ignore[arg-type]
            tools,
            tool_protocol="legacy_json",
            **kwargs,
        )


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
        self.tools = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(),
                approver=lambda _action, _detail: True,
                timeout=5,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_audit_failure_after_tool_keeps_reusable_complete_turn(self) -> None:
        """防止审计失败让已执行工具只留下孤立 assistant 动作。"""
        provider = ScriptedProvider([action("read_file", {"path": "sample.py"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2, audit=FailingAudit())

        failed = agent.run_with_context("读取模块", SessionContext())

        self.assertFalse(failed.result.ok)
        self.assertEqual(
            ["task", "generic", "tool_result"],
            [message.kind for message in failed.context.messages],
        )
        resumed_provider = ScriptedProvider([action("finish", {"summary": "继续完成"})])
        resumed = LegacyCodingAgent(resumed_provider, self.tools, max_rounds=2).run_with_context(
            "继续任务", failed.context
        )
        self.assertTrue(resumed.result.ok)
        self.assertTrue(
            any(message.kind == "tool_result" for message in resumed_provider.histories[0])
        )

    def test_audit_failure_after_finish_keeps_complete_turn(self) -> None:
        """防止 finish 的审计失败遗漏其对应 tool-result。"""
        provider = ScriptedProvider([action("finish", {"summary": "完成"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2, audit=FailingAudit())

        failed = agent.run_with_context("结束任务", SessionContext())

        self.assertFalse(failed.result.ok)
        self.assertEqual(
            ["task", "generic", "tool_result"],
            [message.kind for message in failed.context.messages],
        )

    def test_tiny_budget_keeps_fixed_latest_and_drops_old_complete_history(self) -> None:
        """预算只淘汰旧任务块，不得丢弃固定消息或最新任务。"""
        provider = ScriptedProvider([action("finish", {"summary": "完成"})])
        context = SessionContext(
            messages=(
                Message("user", "旧任务", kind="task"),
                Message("assistant", "旧动作"),
                Message("user", "旧工具结果", kind="tool_result"),
            ),
            persisted_summary="恢复摘要",
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2, max_context_chars=1)

        completed = agent.run_with_context("当前任务", context)

        self.assertTrue(completed.result.ok)
        self.assertEqual(1, len(provider.histories))
        request = provider.histories[0]
        contents = [message.content for message in request]
        self.assertEqual("system", request[0].role)
        self.assertIn("持久化会话摘要", request[1].content)
        self.assertIn(CONTEXT_COMPACTION_NOTICE, contents)
        self.assertEqual("用户任务：当前任务", request[-1].content)
        self.assertNotIn("旧任务", contents)
        self.assertNotIn("旧动作", contents)
        self.assertNotIn("旧工具结果", contents)

    def test_provider_failure_rolls_back_task_only_block(self) -> None:
        """防止首次 Provider 失败把未完成的当前任务写入下轮历史。"""
        provider = FailingProvider()
        context = SessionContext(messages=(Message("user", "旧任务", kind="task"),))
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)

        failed = agent.run_with_context("当前任务", context)

        self.assertFalse(failed.result.ok)
        self.assertEqual(context, failed.context)
        self.assertEqual(1, len(provider.histories))

    def test_provider_failure_after_edit_preserves_complete_current_turn(self) -> None:
        """防止第二次模型请求失败时丢失已执行编辑及其验证状态。"""
        provider = FailingOnSecondRequestProvider(
            action(
                "edit_file",
                {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
            )
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3)

        failed = agent.run_with_context("修改模块", SessionContext())

        self.assertFalse(failed.result.ok)
        self.assertEqual(2, len(provider.histories))
        self.assertEqual(
            ["task", "generic", "tool_result"],
            [message.kind for message in failed.context.messages],
        )
        self.assertEqual(("sample.py",), failed.context.modified_files)
        self.assertEqual("待验证", failed.context.verification)

    def test_successful_writes_record_policy_canonical_relative_paths(self) -> None:
        """防止绝对路径或 dotdot 写入把未规范化路径带入会话元数据。"""
        (self.workspace / "sub").mkdir()
        (self.workspace / "other.py").write_text("value = 1\n", encoding="utf-8")
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {
                        "path": str(self.workspace / "sample.py"),
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                    },
                ),
                action(
                    "edit_file",
                    {
                        "path": "sub/../other.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                    },
                ),
                action("run_command", {"command": "python -m compileall -q sample.py other.py"}),
                action("finish", {"summary": "completed"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=5)

        result = agent.run("canonicalize modified files")

        self.assertTrue(result.ok)
        self.assertEqual(("sample.py", "other.py"), result.modified_files)
        self.assertFalse(any(path.is_absolute() for path in map(Path, result.modified_files)))
        self.assertFalse(any(".." in path for path in result.modified_files))

    def test_tiny_budget_still_sends_latest_complete_tool_round(self) -> None:
        """当前任务的完整工具回合即使超预算也必须发送给 Provider。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {
                        "path": "sample.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2\n" + "x" * 3_000,
                    },
                ),
                action("finish", {"summary": "完成"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3, max_context_chars=2_000)

        failed = agent.run_with_context("修改模块", SessionContext())

        self.assertFalse(failed.result.ok)
        self.assertEqual(2, len(provider.histories))
        second_request = provider.histories[1]
        self.assertIn(CONTEXT_COMPACTION_NOTICE, [m.content for m in second_request])
        self.assertEqual(
            ["task", "generic", "tool_result"],
            [message.kind for message in second_request[-3:]],
        )
        self.assertEqual(
            ["task", "generic", "tool_result", "generic", "tool_result"],
            [message.kind for message in failed.context.messages],
        )
        self.assertEqual(("sample.py",), failed.context.modified_files)
        self.assertEqual("待验证", failed.context.verification)

    def test_run_with_context_reuses_complete_tool_history(self) -> None:
        """防止后续任务丢失上一任务的工具请求或工具结果。"""
        provider = ScriptedProvider(
            [
                action("read_file", {"path": "sample.py"}),
                action("finish", {"summary": "检查完成"}),
                action("finish", {"summary": "继续完成"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3)

        first = agent.run_with_context("检查模块", SessionContext())
        second = agent.run_with_context("继续补测试", first.context)

        self.assertTrue(first.result.ok)
        self.assertTrue(second.result.ok)
        self.assertIsInstance(first.context.messages, tuple)
        self.assertGreater(len(second.context.messages), len(first.context.messages))
        self.assertIn("用户任务：检查模块", provider.histories[0][1].content)
        self.assertIn("用户任务：继续补测试", provider.histories[-1][-1].content)
        self.assertTrue(
            any(message.kind == "tool_result" for message in provider.histories[-1])
        )

    def test_persisted_summary_is_sent_without_raw_tool_history(self) -> None:
        """防止恢复会话时把未持久化的工具历史伪造进摘要。"""
        provider = ScriptedProvider([action("finish", {"summary": "继续检查"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)
        context = SessionContext(persisted_summary="此前修改 src/app.py，验证通过")

        agent.run_with_context("继续检查", context)

        first_request = provider.histories[0]
        self.assertIn("持久化会话摘要", first_request[1].content)
        self.assertNotIn("tool_result", first_request[1].content)

    def test_context_compression_keeps_complete_task_blocks(self) -> None:
        """防止压缩跨任务历史时留下半个任务或半个工具回合。"""
        early_task = "过早任务" * 1_000
        context = SessionContext(
            messages=(
                Message("user", early_task, kind="task"),
                Message("assistant", "过早动作"),
                Message("user", "过早工具结果", kind="tool_result"),
                Message("user", "最近任务", kind="task"),
                Message("assistant", "最近动作"),
                Message("user", "最近工具结果", kind="tool_result"),
            )
        )
        provider = ScriptedProvider([action("finish", {"summary": "完成"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2, max_context_chars=3_000)

        agent.run_with_context("当前任务", context)

        request = provider.histories[0]
        contents = [message.content for message in request]
        self.assertNotIn(early_task, contents)
        self.assertNotIn("过早动作", contents)
        self.assertNotIn("过早工具结果", contents)
        self.assertIn("最近任务", contents)
        self.assertIn("最近动作", contents)
        self.assertIn("最近工具结果", contents)
        self.assertEqual("用户任务：当前任务", request[-1].content)

    def test_incomplete_external_history_is_dropped_even_when_budget_fits(self) -> None:
        """防止公开上下文把 task-only 或半个工具回合发送给 Provider。"""
        context = SessionContext(
            messages=(
                Message("user", "只有任务", kind="task"),
                Message("user", "带孤立动作", kind="task"),
                Message("assistant", "孤立动作"),
                Message("user", "带孤立结果", kind="task"),
                Message("user", "孤立工具结果", kind="tool_result"),
            )
        )
        provider = ScriptedProvider([action("finish", {"summary": "完成"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)

        agent.run_with_context("当前任务", context)

        contents = [message.content for message in provider.histories[0]]
        self.assertEqual([LEGACY_SYSTEM_PROMPT, "用户任务：当前任务"], contents)

    def test_complete_external_history_is_kept_when_budget_fits(self) -> None:
        """防止历史规范化误删合法的完整工具回合。"""
        context = SessionContext(
            messages=(
                Message("user", "旧任务", kind="task"),
                Message("assistant", "旧动作"),
                Message("user", "旧工具结果", kind="tool_result"),
            )
        )
        provider = ScriptedProvider([action("finish", {"summary": "完成"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)

        agent.run_with_context("当前任务", context)

        contents = [message.content for message in provider.histories[0]]
        self.assertEqual(
            [
                LEGACY_SYSTEM_PROMPT,
                "旧任务",
                "旧动作",
                "旧工具结果",
                "用户任务：当前任务",
            ],
            contents,
        )

    def test_context_preserves_modified_files_and_verification_between_turns(self) -> None:
        """防止第二轮遗忘首轮修改，从而错误沿用或绕过验证状态。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("finish", {"summary": "已修改"}),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action("finish", {"summary": "已验证"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3)

        first = agent.run_with_context("修改模块", SessionContext())
        second = agent.run_with_context("验证修改", first.context)

        self.assertFalse(first.result.ok)
        self.assertEqual(("sample.py",), first.context.modified_files)
        self.assertEqual("待验证", first.context.verification)
        self.assertTrue(second.result.ok)
        self.assertEqual(("sample.py",), second.result.modified_files)
        self.assertEqual("通过", second.context.verification)

    def test_empty_context_is_isolated_and_run_still_returns_run_result(self) -> None:
        """防止新会话共享旧会话状态，或破坏原有 run 返回类型。"""
        provider = ScriptedProvider(
            [
                action("finish", {"summary": "旧会话未验证"}),
                action("finish", {"summary": "独立完成"}),
                action("finish", {"summary": "兼容完成"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)

        previous = agent.run_with_context(
            "旧会话", SessionContext(modified_files=("sample.py",), verification="待验证")
        )
        independent = agent.run_with_context("独立任务", SessionContext())
        compatible = agent.run("分析项目")

        self.assertFalse(previous.result.ok)
        self.assertEqual((), independent.context.modified_files)
        self.assertEqual("未运行", independent.context.verification)
        self.assertTrue(independent.result.ok)
        self.assertIsInstance(compatible, RunResult)

    def test_runs_read_edit_check_finish_loop(self) -> None:
        """防止工具结果未回填导致模型无法完成连续编码任务。"""
        provider = ScriptedProvider(
            [
                action("read_file", {"path": "sample.py"}),
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action("finish", {"summary": "修改并检查完成"}),
            ]
        )
        audit_path = self.workspace / "runtime" / "run.jsonl"
        agent = LegacyCodingAgent(
            provider,
            self.tools,
            max_rounds=6,
            audit=AuditLogger(audit_path),
        )

        result = agent.run("把值改成 2")

        self.assertTrue(result.ok)
        self.assertEqual("修改并检查完成", result.summary)
        self.assertEqual(4, result.rounds)
        self.assertEqual(4, result.tool_calls)
        self.assertEqual(("sample.py",), result.modified_files)
        self.assertEqual("通过", result.verification)
        self.assertEqual("value = 2\n", (self.workspace / "sample.py").read_text(encoding="utf-8"))
        third_history = provider.histories[2]
        self.assertTrue(any("已修改 sample.py" in message.content for message in third_history))
        self.assertGreaterEqual(len(audit_path.read_text(encoding="utf-8").splitlines()), 4)

    def test_audit_trail_does_not_duplicate_source_content(self) -> None:
        """防止读取或编辑工具把私有源码正文复制到运行轨迹。"""
        provider = ScriptedProvider(
            [
                action("read_file", {"path": "sample.py"}),
                action(
                    "edit_file",
                    {
                        "path": "sample.py",
                        "old_text": "value = 1",
                        "new_text": "value = 987654",
                    },
                ),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action("finish", {"summary": "完成"}),
            ]
        )
        audit_path = self.workspace / "runtime" / "private-safe.jsonl"
        agent = LegacyCodingAgent(
            provider,
            self.tools,
            max_rounds=4,
            audit=AuditLogger(audit_path),
        )

        result = agent.run("修改示例")
        trail = audit_path.read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        self.assertNotIn("value = 1", trail)
        self.assertNotIn("value = 987654", trail)
        self.assertIn('"path": "sample.py"', trail)
        self.assertIn('"output_chars"', trail)

    def test_audit_stores_reason_and_command_as_structured_metadata_only(self) -> None:
        """防止模型理由或命令参数中的敏感自由文本进入 JSONL。"""
        reason_sentinel = "REASON-SENTINEL-PRIVATE-KEY"
        command_sentinel = "COMMAND-SENTINEL-SOURCE.py"
        provider = ScriptedProvider(
            [
                action(
                    "run_command",
                    {"command": f"python -m compileall -q {command_sentinel}"},
                    reason=reason_sentinel,
                ),
                action("finish", {"summary": "完成"}, reason="结束"),
            ]
        )
        audit_path = self.workspace / "runtime" / "structured-command.jsonl"
        agent = LegacyCodingAgent(
            provider,
            self.tools,
            max_rounds=2,
            audit=AuditLogger(audit_path),
        )

        agent.run("验证审计结构")
        trail = audit_path.read_text(encoding="utf-8")
        events = [json.loads(line) for line in trail.splitlines()]
        command_event = next(event for event in events if event.get("tool") == "run_command")

        self.assertNotIn(reason_sentinel, trail)
        self.assertNotIn(command_sentinel, trail)
        self.assertNotIn("reason", command_event)
        self.assertEqual(len(reason_sentinel), command_event["reason_chars"])
        self.assertNotIn("command", command_event["arguments"])
        self.assertEqual("python", command_event["arguments"]["executable"])
        self.assertEqual("compileall", command_event["arguments"]["python_module"])
        self.assertEqual(4, command_event["arguments"]["argument_count"])

    def test_audit_does_not_store_provider_error_free_text(self) -> None:
        """防止 Provider 异常中的请求片段或凭据哨兵被复制到审计日志。"""
        sentinel = "PROVIDER-ERROR-SENTINEL-PRIVATE"

        class FailingProvider:
            def complete(
                self,
                _messages: list[Message],
                _tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
            ) -> ProviderResponse:
                raise ProviderError(sentinel)

        audit_path = self.workspace / "runtime" / "provider-error.jsonl"
        agent = LegacyCodingAgent(
            FailingProvider(),
            self.tools,
            max_rounds=1,
            audit=AuditLogger(audit_path),
        )

        result = agent.run("触发安全错误")
        trail = audit_path.read_text(encoding="utf-8")

        self.assertFalse(result.ok)
        self.assertNotIn(sentinel, trail)
        event = json.loads(trail)
        self.assertEqual(len(sentinel), event["error_chars"])
        self.assertNotIn("error", event)

    def test_audit_does_not_store_unknown_argument_names(self) -> None:
        """防止未知工具把模型自造的参数名作为自由文本写入审计。"""
        sentinel = "UNKNOWN-ARGUMENT-SENTINEL-PRIVATE"
        provider = ScriptedProvider(
            [
                action("unknown_tool", {sentinel: "value"}),
                action("finish", {"summary": "完成"}),
            ]
        )
        audit_path = self.workspace / "runtime" / "unknown-argument.jsonl"
        agent = LegacyCodingAgent(
            provider,
            self.tools,
            max_rounds=2,
            audit=AuditLogger(audit_path),
        )

        agent.run("触发未知工具")
        trail = audit_path.read_text(encoding="utf-8")
        event = json.loads(trail.splitlines()[0])

        self.assertNotIn(sentinel, trail)
        self.assertEqual("unknown", event["tool"])
        self.assertEqual({"argument_count": 1}, event["arguments"])

    def test_invalid_command_cwd_is_audited_without_crashing_or_leaking_text(
        self,
    ) -> None:
        """非法 cwd 的工具失败也必须生成结构化审计并允许 Agent 安全继续。"""
        sentinel = "INVALID-CWD-SENTINEL-PRIVATE"
        provider = ScriptedProvider(
            [
                action(
                    "run_command",
                    {
                        "command": "python -m unittest",
                        "cwd": f"../{sentinel}",
                    },
                ),
                action("finish", {"summary": "已处理失败"}),
            ]
        )
        audit_path = self.workspace / "runtime" / "invalid-cwd.jsonl"
        agent = LegacyCodingAgent(
            provider,
            self.tools,
            max_rounds=2,
            audit=AuditLogger(audit_path),
        )

        try:
            result = agent.run("尝试非法目录后结束")
        except NameError as exc:
            self.fail(f"非法 cwd 触发未处理的审计异常：{exc}")
        trail = audit_path.read_text(encoding="utf-8")
        event = json.loads(trail.splitlines()[0])

        self.assertTrue(result.ok)
        self.assertNotIn(sentinel, trail)
        self.assertEqual({"valid": False, "chars": len(f"../{sentinel}")}, event["arguments"]["cwd"])

    def test_create_file_records_modified_file_and_safe_audit_parameters(self) -> None:
        """防止新建文件未计入结果，或审计记录泄露完整文件内容。"""
        provider = ScriptedProvider(
            [
                action("create_file", {"path": "created.py", "content": "secret = 987654\n"}),
                action("run_command", {"command": "python -m compileall -q created.py"}),
                action("finish", {"summary": "创建完成"}),
            ]
        )
        audit_path = self.workspace / "runtime" / "create-file.jsonl"
        agent = LegacyCodingAgent(
            provider,
            self.tools,
            max_rounds=3,
            audit=AuditLogger(audit_path),
        )

        result = agent.run("创建文件")
        events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        create_event = next(event for event in events if event.get("tool") == "create_file")

        self.assertTrue(result.ok)
        self.assertEqual(("created.py",), result.modified_files)
        self.assertEqual("secret = 987654\n", (self.workspace / "created.py").read_text(encoding="utf-8"))
        self.assertEqual(
            {"path": "created.py", "content_chars": 16},
            create_event["arguments"],
        )
        self.assertNotIn("secret = 987654", audit_path.read_text(encoding="utf-8"))

    def test_finish_without_file_modification_succeeds(self) -> None:
        """防止无文件修改的只读任务被错误地要求运行验证命令。"""
        provider = ScriptedProvider([action("finish", {"summary": "已完成检查"})])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)

        result = agent.run("检查工作区")

        self.assertTrue(result.ok)
        self.assertEqual("未运行", result.verification)

    def test_finish_after_unverified_file_edit_fails(self) -> None:
        """防止文件编辑成功后未验证仍被 finish 报告为成功。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("finish", {"summary": "已修改"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3)

        result = agent.run("修改示例")

        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)
        self.assertIn("尚未运行验证命令", result.summary)

    def test_finish_after_failed_verification_fails(self) -> None:
        """防止验证命令失败后仍将修改任务报告为成功。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("run_command", {"command": "python -c \"import sys; sys.exit(1)\""}),
                action("finish", {"summary": "已修改"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)

        result = agent.run("修改示例")

        self.assertFalse(result.ok)
        self.assertEqual("失败", result.verification)
        self.assertIn("验证失败", result.summary)

    def test_finish_after_successful_verification_succeeds(self) -> None:
        """防止已修改且验证通过的任务被错误地判定为失败。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action("finish", {"summary": "已修改并验证"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)

        result = agent.run("修改示例")

        self.assertTrue(result.ok)
        self.assertEqual("通过", result.verification)

    def test_edit_after_successful_verification_requires_new_verification(self) -> None:
        """防止验证通过后再次修改文件仍沿用旧的通过状态。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 2", "new_text": "value = 3"},
                ),
                action("finish", {"summary": "再次修改"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=5)

        result = agent.run("连续修改示例")

        self.assertFalse(result.ok)
        self.assertEqual("待验证", result.verification)
        self.assertIn("尚未运行验证命令", result.summary)

    def test_committed_create_with_cleanup_warning_requires_new_verification(self) -> None:
        """防止已发布文件因临时文件清理失败而漏记修改并沿用旧验证状态。"""
        provider = ScriptedProvider(
            [
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action(
                    "create_file",
                    {"path": "committed.py", "content": "committed = True\n"},
                ),
                action("finish", {"summary": "创建完成"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)
        target = self.workspace / "committed.py"
        real_unlink = os.unlink

        def fail_committed_temp(path: object, *args: object, **kwargs: object) -> None:
            if target.exists() and str(path).endswith(".tmp"):
                raise PermissionError("simulated cleanup failure")
            real_unlink(path, *args, **kwargs)

        with patch("tricoder.tools.os.unlink", side_effect=fail_committed_temp):
            result = agent.run("先验证再创建文件")

        self.assertFalse(result.ok)
        self.assertEqual(("committed.py",), result.modified_files)
        self.assertEqual("待验证", result.verification)
        self.assertIn("尚未运行验证命令", result.summary)
        self.assertTrue(target.exists())

    def test_committed_edit_with_close_warning_is_recorded_and_requires_verification(
        self,
    ) -> None:
        """关闭警告不得使 Agent 漏记已经原子替换的文件。"""
        provider = ScriptedProvider(
            [
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action(
                    "edit_file",
                    {
                        "path": "sample.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                    },
                ),
                action("finish", {"summary": "编辑完成"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)

        with patch_binding_close_failure():
            result = agent.run("先验证再编辑文件")

        self.assertFalse(result.ok)
        self.assertEqual(("sample.py",), result.modified_files)
        self.assertEqual("待验证", result.verification)
        self.assertIn("尚未运行验证命令", result.summary)
        self.assertEqual(
            "value = 2\n",
            (self.workspace / "sample.py").read_text(encoding="utf-8"),
        )

    def test_committed_create_with_close_warning_is_recorded_and_requires_verification(
        self,
    ) -> None:
        """关闭警告不得使 Agent 漏记已经硬链接发布的文件。"""
        provider = ScriptedProvider(
            [
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action(
                    "create_file",
                    {"path": "close-warning.py", "content": "committed = True\n"},
                ),
                action("finish", {"summary": "创建完成"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)
        target = self.workspace / "close-warning.py"

        with patch_binding_close_failure():
            result = agent.run("先验证再创建文件")

        self.assertFalse(result.ok)
        self.assertEqual(("close-warning.py",), result.modified_files)
        self.assertEqual("待验证", result.verification)
        self.assertIn("尚未运行验证命令", result.summary)
        self.assertEqual("committed = True\n", target.read_text(encoding="utf-8"))

    def test_failed_edit_does_not_invalidate_previous_successful_verification(self) -> None:
        """失败的 edit_file 没有写入，不得使此前的通过状态失效。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "missing", "new_text": "value = 3"},
                ),
                action("finish", {"summary": "保留已验证修改"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)

        result = agent.run("验证后尝试失败编辑")

        self.assertTrue(result.ok)
        self.assertEqual("通过", result.verification)
        self.assertEqual(("sample.py",), result.modified_files)
        self.assertEqual("value = 2\n", (self.workspace / "sample.py").read_text(encoding="utf-8"))

    def test_failed_create_does_not_invalidate_previous_successful_verification(self) -> None:
        """失败的 create_file 没有写入，不得使此前的通过状态失效。"""
        provider = ScriptedProvider(
            [
                action(
                    "edit_file",
                    {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
                ),
                action("run_command", {"command": "python -m compileall -q sample.py"}),
                action(
                    "create_file",
                    {"path": "sample.py", "content": "value = 3\n"},
                ),
                action("finish", {"summary": "保留已验证修改"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=4)

        result = agent.run("验证后尝试失败创建")

        self.assertTrue(result.ok)
        self.assertEqual("通过", result.verification)
        self.assertEqual(("sample.py",), result.modified_files)
        self.assertEqual("value = 2\n", (self.workspace / "sample.py").read_text(encoding="utf-8"))

    def test_invalid_json_is_returned_to_model_for_correction(self) -> None:
        """防止单次格式错误直接中止一个本可修正的任务。"""
        provider = ScriptedProvider(
            [
                "这不是 JSON",
                action("finish", {"summary": "已修正格式"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3)

        result = agent.run("检查格式")

        self.assertTrue(result.ok)
        self.assertTrue(
            any("动作格式错误" in message.content for message in provider.histories[1])
        )

    def test_unknown_tool_result_is_returned_to_model(self) -> None:
        """防止模型调用未知工具时出现未处理异常。"""
        provider = ScriptedProvider(
            [
                action("delete_everything", {}),
                action("finish", {"summary": "改用安全方案"}),
            ]
        )
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3)

        result = agent.run("安全完成")

        self.assertTrue(result.ok)
        self.assertTrue(any("未知工具" in message.content for message in provider.histories[1]))

    def test_stops_after_max_rounds(self) -> None:
        """防止异常模型造成无限调用和失控费用。"""
        provider = ScriptedProvider(["bad", "bad", "bad"])
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=2)

        result = agent.run("不会结束的任务")

        self.assertFalse(result.ok)
        self.assertEqual(2, result.rounds)
        self.assertIn("最大轮数", result.summary)
        self.assertEqual(2, len(provider.histories))

    def test_parse_action_requires_public_reason_not_hidden_thought(self) -> None:
        """防止动作协议接受缺失字段或非对象参数。"""
        with self.assertRaisesRegex(ValueError, "reason"):
            parse_action('{"tool":"read_file","arguments":{"path":"a"}}')
        with self.assertRaisesRegex(ValueError, "arguments"):
            parse_action('{"tool":"read_file","arguments":[],"reason":"查看"}')

    def test_observer_receives_ordered_agent_events(self) -> None:
        """防止 UI 只能轮询核心状态或丢失工具执行顺序。"""
        provider = ScriptedProvider(
            [
                action("read_file", {"path": "sample.py"}),
                action("finish", {"summary": "完成"}),
            ]
        )
        observer = RecordingObserver()
        agent = LegacyCodingAgent(provider, self.tools, max_rounds=3, observer=observer)

        result = agent.run("读取后结束")

        self.assertTrue(result.ok)
        self.assertEqual(
            [
                "round:1/3",
                "action:read_file",
                "result:read_file:True",
                "round:2/3",
                "action:finish",
                "result:finish:True",
            ],
            observer.events,
        )

    def test_compact_messages_returns_a_new_unchanged_sequence_when_budget_fits(self) -> None:
        """防止预算充足时压缩函数改变消息顺序或原输入列表。"""
        messages = [
            Message("system", "规则"),
            Message("user", "任务"),
            Message("assistant", "旧回答"),
            Message("user", "旧工具结果"),
        ]

        compacted = compact_messages(messages, 100)

        self.assertEqual(messages, compacted)
        self.assertIsNot(messages, compacted)
        self.assertEqual(
            [
                Message("system", "规则"),
                Message("user", "任务"),
                Message("assistant", "旧回答"),
                Message("user", "旧工具结果"),
            ],
            messages,
        )

    def test_compact_messages_keeps_fixed_and_latest_messages_when_over_budget(self) -> None:
        """防止压缩时丢失固定提示、最新完整回合或压缩说明。"""
        messages = [
            Message("system", "固定规则"),
            Message("user", "固定任务"),
            Message("assistant", "很早的回答" * 20),
            Message("user", "较早的工具结果" * 20),
            Message("assistant", "最新回答"),
            Message("user", "最新工具结果"),
        ]
        max_chars = sum(len(message.role) + len(message.content) for message in [
            messages[0],
            messages[1],
            Message("system", CONTEXT_COMPACTION_NOTICE),
            messages[-2],
            messages[-1],
        ])

        compacted = compact_messages(messages, max_chars)

        self.assertEqual(
            [
                messages[0],
                messages[1],
                Message("system", CONTEXT_COMPACTION_NOTICE),
                messages[-2],
                messages[-1],
            ],
            compacted,
        )

    def test_compact_messages_allows_only_fixed_messages_to_exceed_budget(self) -> None:
        """防止固定 system/user 自身超预算时被截断或凭空插入说明。"""
        messages = [
            Message("system", "固定规则超过预算"),
            Message("user", "固定任务超过预算"),
        ]

        compacted = compact_messages(messages, 1)

        self.assertEqual(messages, compacted)
        self.assertIsNot(messages, compacted)
        self.assertNotIn(Message("system", CONTEXT_COMPACTION_NOTICE), compacted)
        self.assertEqual(
            [
                Message("system", "固定规则超过预算"),
                Message("user", "固定任务超过预算"),
            ],
            messages,
        )

    def test_compact_messages_recalculates_budget_after_inserting_notice(self) -> None:
        """防止压缩说明重复扣减成本而丢弃仍能容纳的最近完整回合。"""
        messages = [
            Message("system", "固定规则"),
            Message("user", "固定任务"),
            Message("assistant", "过早消息" * 20),
            Message("user", "过早工具结果" * 20),
            Message("assistant", "最新回答"),
            Message("user", "最新工具结果"),
        ]
        max_chars = sum(len(message.role) + len(message.content) for message in [
            messages[0],
            messages[1],
            Message("system", CONTEXT_COMPACTION_NOTICE),
            messages[4],
            messages[5],
        ])

        compacted = compact_messages(messages, max_chars)

        self.assertEqual(
            [
                messages[0],
                messages[1],
                Message("system", CONTEXT_COMPACTION_NOTICE),
                messages[4],
                messages[5],
            ],
            compacted,
        )

    def test_compact_messages_never_retains_an_orphan_half_round(self) -> None:
        """防止只保留 assistant 动作而丢失对应 tool_result，或反之。"""
        messages = [
            Message("system", "固定规则"),
            Message("user", "固定任务"),
            Message("assistant", "完整动作"),
            Message("user", "完整工具结果"),
            Message("assistant", "没有工具结果的孤立动作" * 20),
        ]
        max_chars = sum(
            _message_chars
            for _message_chars in (
                len(messages[0].role) + len(messages[0].content),
                len(messages[1].role) + len(messages[1].content),
                len("system") + len(CONTEXT_COMPACTION_NOTICE),
                len(messages[2].role) + len(messages[2].content),
                len(messages[3].role) + len(messages[3].content),
            )
        )

        compacted = compact_messages(messages, max_chars)

        self.assertEqual(
            [
                messages[0],
                messages[1],
                Message("system", CONTEXT_COMPACTION_NOTICE),
                messages[2],
                messages[3],
            ],
            compacted,
        )

    def test_compact_messages_drops_orphan_half_round_even_when_budget_fits(
        self,
    ) -> None:
        """孤立动作不是完整交互，预算充足也必须淘汰并插入说明。"""
        messages = [
            Message("system", "固定规则"),
            Message("user", "固定任务"),
            Message("assistant", "没有工具结果的孤立动作"),
        ]

        compacted = compact_messages(messages, 10_000)

        self.assertEqual(
            [
                messages[0],
                messages[1],
                Message("system", CONTEXT_COMPACTION_NOTICE),
            ],
            compacted,
        )
        self.assertEqual(3, len(messages))

    def test_compact_messages_drops_oversized_latest_round_as_a_unit(self) -> None:
        """防止最新回合放不下时保留半回合或倒退保留更旧历史。"""
        messages = [
            Message("system", "固定规则"),
            Message("user", "固定任务"),
            Message("assistant", "旧动作"),
            Message("user", "旧结果"),
            Message("assistant", "最新动作" * 100),
            Message("user", "最新结果" * 100),
        ]
        max_chars = sum(len(message.role) + len(message.content) for message in [
            messages[0],
            messages[1],
            Message("system", CONTEXT_COMPACTION_NOTICE),
            messages[2],
            messages[3],
        ])

        compacted = compact_messages(messages, max_chars)

        self.assertEqual(
            [
                messages[0],
                messages[1],
                Message("system", CONTEXT_COMPACTION_NOTICE),
            ],
            compacted,
        )

    def test_compact_messages_keeps_newest_rounds_in_original_order_at_boundary(
        self,
    ) -> None:
        """精确预算边界应保留两个最新完整回合，并恢复原始先后顺序。"""
        messages = [
            Message("system", "固定规则"),
            Message("user", "固定任务"),
            Message("assistant", "动作一" * 20),
            Message("user", "结果一" * 20),
            Message("assistant", "动作二"),
            Message("user", "结果二"),
            Message("assistant", "动作三"),
            Message("user", "结果三"),
        ]
        expected = [
            messages[0],
            messages[1],
            Message("system", CONTEXT_COMPACTION_NOTICE),
            messages[4],
            messages[5],
            messages[6],
            messages[7],
        ]
        max_chars = sum(len(message.role) + len(message.content) for message in expected)

        compacted = compact_messages(messages, max_chars)

        self.assertEqual(expected, compacted)
        self.assertEqual("动作二", compacted[3].content)
        self.assertEqual("结果三", compacted[-1].content)

    def test_coding_agent_rejects_non_positive_max_context_chars(self) -> None:
        """防止 Agent 接受无效预算并在 Provider 调用前产生不可预测行为。"""
        provider = ScriptedProvider([])

        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_context_chars"):
                    LegacyCodingAgent(provider, self.tools, max_context_chars=value)


if __name__ == "__main__":
    unittest.main()
