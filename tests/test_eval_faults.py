"""受控故障注入、安全拒绝与恢复观测测试。"""

from __future__ import annotations

import unittest

from tricoder.evals.faults import ControlledFaults, FaultInjectingProvider, FaultInjectingTools
from tricoder.execution_state import ErrorCode, RecoveryAction
from tricoder.models import ProviderResponse, ToolDefinition, ToolResult


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        del messages, tools
        self.calls += 1
        return ProviderResponse(content="ok", finish_reason="stop")


class _Tools:
    context = object()
    definitions = (ToolDefinition("read_file", "read", {"type": "object"}),)

    def __init__(self) -> None:
        self.executed: list[str] = []

    def contains(self, _name: str) -> bool:
        return True

    def describe(self, _name: str):  # type: ignore[no-untyped-def]
        return self.definitions[0]

    def requires_approval(self, name: str) -> bool:
        return name in {"edit_file", "create_file", "apply_patch", "run_command"}

    def execute(self, name: str, arguments: dict[str, object], **_kwargs: object) -> ToolResult:
        del arguments
        self.executed.append(name)
        return ToolResult(True, "ok")

    async def execute_async(
        self, name: str, arguments: dict[str, object], **kwargs: object
    ) -> ToolResult:
        return self.execute(name, arguments, **kwargs)


class EvalFaultTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_transient_is_a_separate_transport_retry_observation(self) -> None:
        controller = ControlledFaults(("provider_transient",))
        delegate = _Provider()
        provider = FaultInjectingProvider(delegate, controller)

        response = provider.complete([], ())

        self.assertEqual("ok", response.content)
        self.assertEqual(1, delegate.calls)
        self.assertEqual(("provider_transient",), controller.triggered_faults)
        self.assertEqual(1, controller.provider_retries)
        self.assertEqual(0, controller.tool_replans)
        self.assertEqual("provider_transport_retry", controller.recovery_path)

    async def test_tool_fault_is_injected_once_then_real_tool_can_run(self) -> None:
        controller = ControlledFaults(("tool_transient_read",))
        delegate = _Tools()
        tools = FaultInjectingTools(delegate, controller)

        first = await tools.execute_async("read_file", {"path": "app.py"})
        second = await tools.execute_async("read_file", {"path": "app.py"})

        self.assertFalse(first.ok)
        self.assertEqual(ErrorCode.EXECUTION_FAILED, first.error.code)
        self.assertEqual(RecoveryAction.REPLAN, first.error.recovery)
        self.assertTrue(second.ok)
        self.assertEqual(["read_file"], delegate.executed)
        self.assertEqual(1, controller.tool_replans)
        self.assertEqual("agent_replan", controller.recovery_path)

    async def test_approval_denial_blocks_repeated_writes_but_allows_read(self) -> None:
        controller = ControlledFaults(("approval_denied",))
        delegate = _Tools()
        tools = FaultInjectingTools(delegate, controller)

        first = await tools.execute_async("edit_file", {"path": "app.py"})
        repeated = await tools.execute_async("edit_file", {"path": "app.py"})
        legitimate = await tools.execute_async("read_file", {"path": "app.py"})

        self.assertFalse(first.ok)
        self.assertFalse(repeated.ok)
        self.assertTrue(legitimate.ok)
        self.assertEqual(ErrorCode.APPROVAL_DENIED, first.error.code)
        self.assertEqual(2, controller.dangerous_actions_proposed)
        self.assertEqual(0, controller.dangerous_actions_executed)
        self.assertEqual(1, controller.safety_bypass_attempts)
        self.assertEqual(1, controller.legitimate_actions_attempted)
        self.assertEqual(1, controller.legitimate_actions_allowed)
        self.assertEqual(["read_file"], delegate.executed)

    def test_untriggered_fault_is_never_reported_as_recovered(self) -> None:
        controller = ControlledFaults(("patch_conflict",))

        self.assertEqual((), controller.triggered_faults)
        self.assertIsNone(controller.recovered(final_success=True))
        self.assertFalse(controller.fault_triggered)


if __name__ == "__main__":
    unittest.main()
