"""命令兼容、执行目录一致性与可恢复错误的跨层回归。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.core.cancellation import CancellationError
from tricoder.execution_state import EffectState, ErrorCode, RecoveryAction
from tricoder.extensions import ToolOrigin
from tricoder.models import (
    ProviderResponse,
    SessionContext,
    ToolAction,
    ToolCall,
    ToolResult,
)
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy
from tricoder.process.control import BoundedProcessResult, ProcessExecutionUncertain
from tricoder.protocols import LegacyJsonProtocol, NativeToolProtocol
from tricoder.task_observation import apply_tool_transition
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.tools import command as command_module
from tricoder.tools.handlers import ToolHandler
from tricoder.workspace.verification import VerificationScope


class CommandCompatibilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "test_calculator.py").write_text(
            "raise AssertionError('策略校验不应导入测试模块')\n",
            encoding="utf-8",
        )
        (self.workspace / "helper.py").write_text("print('root')\n", encoding="utf-8")
        self.nested = self.workspace / "nested"
        self.nested.mkdir()
        (self.nested / "helper.py").write_text("print('nested')\n", encoding="utf-8")
        (self.nested / "test_nested.py").write_text("import unittest\n", encoding="utf-8")
        (self.nested / "tests").mkdir()
        self.policy = CommandPolicy(self.workspace)

    def test_fixed_version_query_uses_isolated_trusted_python(self) -> None:
        """若版本查询仍走脚本校验，受支持的诊断命令会被误拒绝。"""

        for command in ("python --version", "python -V"):
            with self.subTest(command=command):
                args = self.policy.validate(command)
                self.assertEqual(Path(sys.executable).resolve(), Path(args[0]).resolve())
                self.assertEqual(["-I", "--version"], args[1:])

    def test_python3_unittest_simple_module_maps_to_local_file(self) -> None:
        """若别名或本地模块简写未归一化，截图中的正常测试命令会被拒绝。"""

        args = self.policy.validate("python3 -m unittest -v test_calculator")

        self.assertEqual(Path(sys.executable).resolve(), Path(args[0]).resolve())
        self.assertEqual(
            ["-m", "unittest", "-v", "test_calculator.py"],
            args[1:],
        )

    def test_all_supported_python_names_use_the_same_trusted_interpreter(self) -> None:
        """若别名各自查询 PATH，工作区同名程序可能改变真实执行来源。"""

        for alias in ("python", "python.exe", "py", "py.exe", "python3", "python3.exe"):
            with self.subTest(alias=alias):
                args = self.policy.validate(f"{alias} --version")
                self.assertEqual(Path(sys.executable).resolve(), Path(args[0]).resolve())
                self.assertEqual(["-I", "--version"], args[1:])

    def test_unsupported_python_names_and_version_forms_remain_denied(self) -> None:
        """兼容别名不得演变成任意版本选择、任意代码或附加参数入口。"""

        commands = (
            "python3.12 --version",
            "python2 --version",
            "py -3.12 --version",
            "python --version helper.py",
            "python -V --verbose",
            "python3 -c print(1)",
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(PolicyError):
                    self.policy.validate(command)

    def test_paths_are_resolved_against_effective_cwd_without_state_leakage(self) -> None:
        """若策略仍按根目录检查或缓存 cwd，检查与执行会指向不同文件。"""

        nested_script = self.policy.validate("python helper.py", cwd=self.nested)
        nested_test = self.policy.validate(
            "python -m unittest -v test_nested",
            cwd=self.nested,
        )
        nested_discovery = self.policy.validate(
            "python -m unittest discover -s tests -v",
            cwd=self.nested,
        )
        root_script = self.policy.validate("python helper.py", cwd=self.workspace)

        self.assertEqual(["helper.py"], nested_script[1:])
        self.assertEqual(["-m", "unittest", "-v", "test_nested.py"], nested_test[1:])
        self.assertEqual(
            ["-m", "unittest", "discover", "-s", "tests", "-v"],
            nested_discovery[1:],
        )
        self.assertEqual(["helper.py"], root_script[1:])
        with self.assertRaises(PolicyError):
            self.policy.validate("python -m unittest -v test_nested", cwd=self.workspace)

    def test_simple_unittest_target_requires_workspace_proof(self) -> None:
        """没有工作区边界时不得借进程 cwd 猜测模块简写的授权目标。"""

        with self.assertRaises(PolicyError):
            CommandPolicy().validate("python -m unittest -v test_calculator")

    def test_dotted_and_missing_unittest_targets_remain_denied(self) -> None:
        """模块简写只覆盖单段本地文件，不允许 dotted import 或外部搜索。"""

        for target in (
            "missing_test",
            "tests.test_calculator",
            "test_calculator.Example.test_case",
        ):
            with self.subTest(target=target):
                with self.assertRaises(PolicyError):
                    self.policy.validate(f"python -m unittest -v {target}")

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "当前平台不支持符号链接 API")
    def test_simple_unittest_target_rejects_symlink_escape(self) -> None:
        """本地文件简写不能把指向工作区外的链接转换成可执行测试目标。"""

        outside = Path(tempfile.mkdtemp(prefix="tricoder-command-outside-"))
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        (outside / "test_escape.py").write_text("import unittest\n", encoding="utf-8")
        link = self.workspace / "test_escape.py"
        try:
            link.symlink_to(outside / "test_escape.py")
        except OSError:
            (outside / "test_escape.py").unlink(missing_ok=True)
            self.skipTest("当前账户不能创建符号链接")
        self.addCleanup(link.unlink, missing_ok=True)
        self.addCleanup((outside / "test_escape.py").unlink, missing_ok=True)

        with self.assertRaisesRegex(PolicyError, "工作区"):
            self.policy.validate("python -m unittest test_escape")

    def test_runner_receives_the_same_normalized_argv_and_cwd_that_were_checked(self) -> None:
        """只断言策略放行不足以防止工具层执行另一组 argv 或目录。"""

        approvals: list[tuple[str, str]] = []
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                self.policy,
                lambda action, detail: approvals.append((action, detail)) or True,
            )
        )
        completed = BoundedProcessResult(0, "ok", "")

        with patch.object(command_module, "run_bounded_process", return_value=completed) as run:
            result = registry.execute(
                "run_command",
                {
                    "command": "python3 -m unittest -v test_nested",
                    "cwd": "nested",
                },
            )

        self.assertTrue(result.ok, result.output)
        executed = run.call_args.args[0]
        self.assertEqual(Path(sys.executable).resolve(), Path(executed[0]).resolve())
        self.assertEqual(["-m", "unittest", "-v", "test_nested.py"], executed[1:])
        self.assertEqual(self.nested.resolve(), run.call_args.kwargs["cwd"])
        self.assertEqual("run_command", approvals[0][0])
        detail = approvals[0][1]
        self.assertIn("原始请求：python3 -m unittest -v test_nested", detail)
        self.assertIn("归一化 argv：", detail)
        self.assertIn('"test_nested.py"', detail)
        self.assertIn(f"有效目录：{self.nested.resolve()}", detail)
        self.assertIn("会话解释器", detail)

    def test_audit_classifies_information_and_uses_effective_cwd(self) -> None:
        """审计不得把本地构造的 -I 当脚本，也不能回退到错误目录。"""

        information = self.policy.audit_metadata("python3 -V", cwd=self.nested)
        local_test = self.policy.audit_metadata(
            "python -m unittest test_nested",
            cwd=self.nested,
        )
        outside = self.workspace.parent / "outside-command-cwd"
        invalid = self.policy.audit_metadata("python -m unittest test_calculator", cwd=outside)

        self.assertEqual("information", information["execution_kind"])
        self.assertEqual("python_version", information["information_kind"])
        self.assertEqual("module", local_test["execution_kind"])
        self.assertFalse(invalid["command_valid"])

    def test_unbound_policy_scope_refilters_target_workspace_from_path(self) -> None:
        """兼容绑定不能沿用旧 PATH，让工作区同名 git 冒充可信程序。"""

        trusted_git = CommandPolicy().validate("git status")[0]
        fake_name = "git.exe" if os.name == "nt" else "git"
        fake_git = self.workspace / fake_name
        fake_git.write_bytes(b"synthetic executable")
        if os.name != "nt":
            fake_git.chmod(0o755)
        environ = {
            "PATH": os.pathsep.join((str(self.workspace), str(Path(trusted_git).parent))),
            **({"PATHEXT": ".EXE"} if os.name == "nt" else {}),
        }
        scoped = CommandPolicy(environ=environ).scoped_to(
            WorkspacePolicy(self.workspace)
        )

        resolved = scoped.validate("git status")[0]

        self.assertNotEqual(fake_git.resolve(), Path(resolved).resolve())
        path_entries = {
            Path(entry).resolve(strict=False)
            for entry in scoped.subprocess_environment()["PATH"].split(os.pathsep)
            if entry
        }
        self.assertNotIn(self.workspace.resolve(), path_entries)


class InformationCommandEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                lambda *_: True,
            )
        )

    def _execute_version(
        self,
        completed: BoundedProcessResult | BaseException,
    ):
        effect = completed if isinstance(completed, BaseException) else None
        with patch.object(
            command_module,
            "run_bounded_process",
            side_effect=effect,
            return_value=None if effect is not None else completed,
        ):
            return self.registry.execute(
                "run_command",
                {"command": "python --version"},
            )

    def test_successful_version_query_has_no_file_or_verification_effect(self) -> None:
        """只放行命令仍不够：成功查询不得污染 unknown 或伪造验证。"""

        result = self._execute_version(BoundedProcessResult(0, "Python 3.x", ""))

        self.assertTrue(result.ok, result.output)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertFalse(self.registry.context.verification_scope.unknown_effects)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_successful_version_query_preserves_preexisting_unknown_and_failure(self) -> None:
        """信息命令只能不新增影响，不能洗掉既有 UNKNOWN 或失败证据。"""

        scope = self.registry.context.verification_scope
        before = scope.capture(self.registry.context.workspace_policy)
        failed = scope.issue(before, before, False)
        state = SessionContext(
            verification="失败",
            verification_required=True,
            verification_failure=failed.after,
        )
        scope.unknown_effects = True

        result = self._execute_version(BoundedProcessResult(0, "Python 3.x", ""))
        observed = apply_tool_transition(
            state,
            result.file_effects,
            result.verification_evidence,
        )

        # 旧 UNKNOWN 留在作用域中；本次信息查询自身发布 NONE，避免把旧状态
        # 当作新副作用重放到验证状态机。
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertTrue(scope.unknown_effects)
        self.assertEqual("失败", observed.verification)
        self.assertEqual(failed.after, observed.verification_failure)
        self.assertIsNone(observed.verification_evidence)

    def test_abnormal_version_query_never_claims_no_effect(self) -> None:
        """超时、超量、非零退出和清理不确定都保持保守语义。"""

        cases = (
            (BoundedProcessResult(1, "", "failed"), ErrorCode.EXECUTION_FAILED),
            (BoundedProcessResult(None, "", "", timed_out=True), ErrorCode.TIMEOUT),
            (BoundedProcessResult(None, "", "", output_exceeded=True), ErrorCode.OUTPUT_LIMIT),
            (BoundedProcessResult(None, "", "", cleanup_failed=True), ErrorCode.CLEANUP_FAILED),
            (ProcessExecutionUncertain(cleanup_failed=False), ErrorCode.RESULT_UNCERTAIN),
        )
        for completed, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                self.registry.context.verification_scope.unknown_effects = False
                result = self._execute_version(completed)
                self.assertFalse(result.ok)
                self.assertEqual(expected_code, result.error.code)
                self.assertEqual(EffectState.UNKNOWN, result.file_effects.state)
                self.assertTrue(
                    self.registry.context.verification_scope.unknown_effects
                )

    def test_start_failure_restores_state_but_cancellation_after_handoff_does_not(self) -> None:
        """确定未启动可以回滚；已交给进程层的取消不能宣称无副作用。"""

        start_failure = self._execute_version(OSError("not started"))
        self.assertEqual(ErrorCode.EXECUTION_FAILED, start_failure.error.code)
        self.assertFalse(self.registry.context.verification_scope.unknown_effects)

        handler = self.registry._builtin_handlers["run_command"]
        with patch.object(
            command_module,
            "run_bounded_process",
            side_effect=CancellationError("cancelled after process handoff"),
        ):
            with self.assertRaises(CancellationError):
                handler.run_with_cancellation(
                    {"command": "python --version"},
                    None,
                )
        self.assertTrue(self.registry.context.verification_scope.unknown_effects)

    def test_edit_then_only_version_query_cannot_finish_successfully(self) -> None:
        """诊断查询不能替代修改后的验证证据。"""

        actions = iter(
            (
                ("edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}),
                ("run_command", {"command": "python --version"}),
                ("finish", {"summary": "done"}),
            )
        )

        class Provider:
            def complete(self, messages, tools=()):
                tool, arguments = next(actions)
                return ProviderResponse(tool_calls=(ToolCall("call", tool, arguments),))

        with patch.object(
            command_module,
            "run_bounded_process",
            return_value=BoundedProcessResult(0, "Python 3.x", ""),
        ):
            turn = CodingAgent(
                Provider(),
                self.registry,
                plan_enabled=False,
                max_rounds=4,
            ).run_with_context("修改后查询版本", SessionContext())

        self.assertFalse(turn.result.ok)
        self.assertTrue(turn.context.verification_required)
        self.assertIsNone(turn.context.verification_evidence)


class PlainScriptEffectObservationTests(unittest.TestCase):
    """普通脚本要观察文件状态，但不能冒充测试或静态检查。"""

    class _CountingScope(VerificationScope):
        def __init__(self) -> None:
            super().__init__()
            self.capture_count = 0

        def capture(self, policy):
            self.capture_count += 1
            return super().capture(policy)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.scope = self._CountingScope()
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                lambda *_: True,
                verification_scope=self.scope,
            )
        )

    def _write_script(self, name: str, source: str) -> None:
        (self.workspace / name).write_text(source, encoding="utf-8")

    def test_plain_script_stable_workspace_does_not_create_unknown(self) -> None:
        """若普通命令仍不采集快照，稳定 Hello 脚本会被错误标记为 UNKNOWN。"""

        self._write_script("hello.py", "print('Hello from TriCoder')\n")

        result = self.registry.execute(
            "run_command",
            {"command": "python hello.py"},
        )

        self.assertTrue(result.ok, result.output)
        self.assertIn("Hello from TriCoder", result.output)
        self.assertEqual(2, self.scope.capture_count)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertFalse(self.scope.unknown_effects)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_plain_script_nonzero_exit_keeps_stable_effects_replannable(self) -> None:
        """非零退出是执行失败，不应在稳定工作区上伪造 UNKNOWN。"""

        self._write_script("fail.py", "raise SystemExit(3)\n")

        result = self.registry.execute(
            "run_command",
            {"command": "python fail.py"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.EXECUTION_FAILED, result.error.code)
        self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertEqual(2, self.scope.capture_count)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_plain_script_write_remains_unknown(self) -> None:
        """若脚本改变受覆盖文件，退出码 0 也不能解除 UNKNOWN。"""

        self._write_script(
            "mutate.py",
            "from pathlib import Path\nPath('generated.py').write_text('changed\\n')\n",
        )

        result = self.registry.execute(
            "run_command",
            {"command": "python mutate.py"},
        )

        self.assertTrue(result.ok, result.output)
        self.assertEqual(EffectState.UNKNOWN, result.file_effects.state)
        self.assertTrue(self.scope.unknown_effects)
        self.assertEqual(2, self.scope.capture_count)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_plain_script_pre_scan_failure_never_starts_process(self) -> None:
        """执行前观察失败必须在进程交付之前停止，不能先运行再猜测。"""

        self._write_script("hello.py", "print('Hello')\n")
        with patch.object(
            self._CountingScope,
            "capture",
            side_effect=OSError("synthetic pre-scan failure"),
        ), patch.object(command_module, "run_bounded_process") as run:
            result = self.registry.execute(
                "run_command",
                {"command": "python hello.py"},
            )

        run.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.RESULT_UNCERTAIN, result.error.code)
        self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
        self.assertEqual(EffectState.UNKNOWN, result.file_effects.state)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_plain_script_post_scan_failure_keeps_unknown(self) -> None:
        """进程已执行后观察失败必须保留 UNKNOWN 并停止。"""

        self._write_script("hello.py", "print('Hello')\n")
        before = VerificationScope.capture(
            self.scope,
            self.registry.context.workspace_policy,
        )
        with patch.object(
            self._CountingScope,
            "capture",
            side_effect=(before, OSError("synthetic post-scan failure")),
        ), patch.object(
            command_module,
            "run_bounded_process",
            return_value=BoundedProcessResult(0, "Hello", ""),
        ) as run:
            result = self.registry.execute(
                "run_command",
                {"command": "python hello.py"},
            )

        run.assert_called_once()
        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.RESULT_UNCERTAIN, result.error.code)
        self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
        self.assertEqual(EffectState.UNKNOWN, result.file_effects.state)
        self.assertTrue(self.scope.unknown_effects)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_plain_script_never_clears_preexisting_unknown(self) -> None:
        """稳定脚本只能恢复进入命令前的 UNKNOWN，不能洗掉它。"""

        self._write_script("hello.py", "print('Hello')\n")
        self.scope.unknown_effects = True

        result = self.registry.execute(
            "run_command",
            {"command": "python hello.py"},
        )
        observed = apply_tool_transition(
            SessionContext(unknown_effects=True, verification="待验证"),
            result.file_effects,
            result.verification_evidence,
        )

        self.assertTrue(result.ok, result.output)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertTrue(self.scope.unknown_effects)
        self.assertTrue(observed.unknown_effects)
        self.assertEqual("待验证", observed.verification)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)

    def test_plain_script_async_path_matches_sync_observation(self) -> None:
        """规范异步入口不能绕过普通脚本的前后观察。"""

        self._write_script("hello.py", "print('Hello async')\n")

        result = asyncio.run(
            self.registry.execute_async(
                "run_command",
                {"command": "python hello.py"},
            )
        )

        self.assertTrue(result.ok, result.output)
        self.assertIn("Hello async", result.output)
        self.assertEqual(2, self.scope.capture_count)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertFalse(self.scope.unknown_effects)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)


class CommandFormRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "test_ok.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
            encoding="utf-8",
        )
        self.approvals: list[tuple[str, str]] = []
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                lambda action, detail: self.approvals.append((action, detail)) or True,
            )
        )

    def test_safe_direct_pytest_is_replan_without_approval_or_process(self) -> None:
        """安全的已知入口应给出固定修正形式，而不是终止整个任务。"""

        with patch.object(command_module, "run_bounded_process") as run:
            result = self.registry.execute(
                "run_command",
                {"command": "pytest -q"},
            )

        self.assertFalse(result.ok)
        self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
        self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
        self.assertIn("python -m pytest", result.output)
        self.assertEqual([], self.approvals)
        run.assert_not_called()

    def test_unsupported_unittest_target_has_fixed_local_target_guidance(self) -> None:
        """缺失简写和 dotted 目标可重规划，但反馈不能回显输入。"""

        for target in ("MISSING_PRIVATE_TARGET", "tests.SECRET_CLASS.test_token"):
            with self.subTest(target=target):
                with patch.object(command_module, "run_bounded_process") as run:
                    result = self.registry.execute(
                        "run_command",
                        {"command": f"python -m unittest -v {target}"},
                    )
                self.assertEqual(ErrorCode.INVALID_ARGUMENT, result.error.code)
                self.assertEqual(RecoveryAction.REPLAN, result.error.recovery)
                self.assertIn("discover", result.output)
                self.assertIn(".py", result.output)
                self.assertNotIn(target, result.output)
                run.assert_not_called()
        self.assertEqual([], self.approvals)

    def test_unsafe_direct_tool_forms_remain_hard_denials(self) -> None:
        """看见 pytest 名称不能先于完整参数与路径安全检查降级。"""

        commands = (
            "pytest --rootdir=PRIVATE_SENTINEL",
            "pytest ../PRIVATE_SENTINEL",
            "pytest .env.local",
            "pytest --ignore=.env.local",
            "pytest -q | whoami",
            "python -m unittest ../PRIVATE_SENTINEL.py",
            "python -m unittest .env.local",
        )
        for command in commands:
            with self.subTest(command=command):
                with patch.object(command_module, "run_bounded_process") as run:
                    result = self.registry.execute(
                        "run_command",
                        {"command": command},
                    )
                self.assertEqual(ErrorCode.POLICY_DENIED, result.error.code)
                self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
                self.assertNotIn("PRIVATE_SENTINEL", result.output)
                run.assert_not_called()
        self.assertEqual([], self.approvals)

    def test_native_batch_replans_then_executes_compliant_command(self) -> None:
        """首轮失败后的写动作只 skipped；下一轮合规命令重新审批和执行。"""

        rounds = iter(
            (
                ProviderResponse(
                    tool_calls=(
                        ToolCall("bad-command", "run_command", {"command": "pytest -q"}),
                        ToolCall("must-skip", "create_file", {"path": "leak.py", "content": "x=1\n"}),
                    )
                ),
                ProviderResponse(
                    tool_calls=(
                        ToolCall(
                            "good-command",
                            "run_command",
                            {"command": "python -m pytest -q"},
                        ),
                    )
                ),
                ProviderResponse(
                    tool_calls=(ToolCall("finish", "finish", {"summary": "done"}),)
                ),
            )
        )

        class Provider:
            def __init__(self) -> None:
                self.requests: list[tuple[object, ...]] = []

            def complete(self, messages, tools=()):
                self.requests.append(tuple(messages))
                return next(rounds)

        provider = Provider()
        with patch.object(
            command_module,
            "run_bounded_process",
            return_value=BoundedProcessResult(0, "1 passed", ""),
        ) as run:
            turn = CodingAgent(
                provider,
                self.registry,
                plan_enabled=False,
                max_rounds=5,
            ).run_with_context("运行测试", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertFalse((self.workspace / "leak.py").exists())
        self.assertEqual(1, run.call_count)
        self.assertEqual(1, len(self.approvals))
        second_request_text = "\n".join(
            message.content or "" for message in provider.requests[1]
        )
        self.assertIn("python -m pytest", second_request_text)
        self.assertIn("status", second_request_text)
        self.assertIn("skipped", second_request_text)

    def test_both_protocols_receive_same_fixed_feedback_without_raw_target(self) -> None:
        """原生与 legacy 只改变消息角色/配对，不改变安全反馈正文。"""

        raw_target = "tests.PRIVATE_SENTINEL.test_secret"
        result = self.registry.execute(
            "run_command",
            {"command": f"python -m unittest {raw_target}"},
        )
        action = ToolAction("run_command", {}, "")

        native = NativeToolProtocol().tool_result_message(action, result, "call")
        legacy = LegacyJsonProtocol().tool_result_message(action, result, None)

        self.assertEqual(native.content, legacy.content)
        self.assertIn("unittest discover", native.content)
        self.assertNotIn(raw_target, native.content)

    def test_extension_cannot_forge_recoverable_local_command_form(self) -> None:
        """普通扩展自报 INVALID_ARGUMENT/REPLAN 不能取得内置预检查权限。"""

        class ForgedRecovery(ToolHandler):
            name = "forged_recovery"
            description = "synthetic"
            parameters = ToolHandler._schema({})

            def run(self, arguments):
                from tricoder.execution_state import ToolError

                return ToolResult(
                    False,
                    "python -m pytest",
                    error=ToolError(
                        ErrorCode.INVALID_ARGUMENT,
                        RecoveryAction.REPLAN,
                    ),
                )

        self.registry.register(
            ForgedRecovery(self.registry.context),
            origin=ToolOrigin("hook", "synthetic", "read"),
        )

        result = self.registry.execute("forged_recovery", {})

        self.assertEqual(ErrorCode.INVALID_RESULT, result.error.code)
        self.assertEqual(RecoveryAction.STOP_TASK, result.error.recovery)
        self.assertNotIn("python -m pytest", result.output)


class CommandGuidanceTests(unittest.TestCase):
    def test_native_and_legacy_prompts_share_command_boundaries(self) -> None:
        """两种协议必须给出相同的可执行形式与停止/重规划边界。"""

        for prompt in (
            NativeToolProtocol.system_prompt,
            LegacyJsonProtocol.system_prompt,
        ):
            with self.subTest(protocol_prompt=prompt[:20]):
                self.assertIn("python -m unittest discover -v", prompt)
                self.assertIn("python -c", prompt)
                self.assertIn("INVALID_ARGUMENT", prompt)
                self.assertIn("POLICY_DENIED", prompt)
                self.assertIn("finish", prompt)

    def test_run_command_description_states_whitelist_and_version_scope(self) -> None:
        """Provider 工具定义不能把受限执行器描述成通用 Shell。"""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name)
        registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(workspace),
                CommandPolicy(workspace),
                lambda *_: False,
            )
        )
        description = next(
            item.description
            for item in registry.definitions
            if item.name == "run_command"
        )

        self.assertIn("白名单", description)
        self.assertIn("python -m unittest discover -v", description)
        self.assertIn("版本", description)


class SyntheticCommandEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n",
            encoding="utf-8",
        )
        (self.workspace / "test_calculator.py").write_text(
            "import unittest\n"
            "from calculator import add\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(3, add(1, 2))\n",
            encoding="utf-8",
        )
        self.approvals: list[tuple[str, str]] = []
        self.registry = ToolRegistry(
            ToolContext(
                WorkspacePolicy(self.workspace),
                CommandPolicy(self.workspace),
                lambda action, detail: self.approvals.append((action, detail)) or True,
                timeout=15,
            )
        )

    @staticmethod
    def _native_provider(actions):
        pending = iter(actions)

        class Provider:
            def __init__(self) -> None:
                self.requests: list[tuple[object, ...]] = []

            def complete(self, messages, tools=()):
                self.requests.append(tuple(messages))
                call_id, tool, arguments = next(pending)
                return ProviderResponse(
                    tool_calls=(ToolCall(call_id, tool, arguments),)
                )

        return Provider()

    @staticmethod
    def _legacy_provider(actions):
        pending = iter(actions)

        class Provider:
            def __init__(self) -> None:
                self.requests: list[tuple[object, ...]] = []

            def complete(self, messages, tools=()):
                self.requests.append(tuple(messages))
                tool, arguments = next(pending)
                return ProviderResponse(
                    content=json.dumps(
                        {"tool": tool, "arguments": arguments, "reason": "合成验证"},
                        ensure_ascii=False,
                    )
                )

        return Provider()

    def test_native_fake_provider_runs_real_alias_version_and_unittest(self) -> None:
        """真实子进程证明别名归一化、测试输出、证据和完成门禁形成闭环。"""

        provider = self._native_provider(
            (
                ("version", "run_command", {"command": "python3 --version"}),
                (
                    "test",
                    "run_command",
                    {"command": "python3 -m unittest -v test_calculator"},
                ),
                ("finish", "finish", {"summary": "synthetic complete"}),
            )
        )
        turn = CodingAgent(
            provider,
            self.registry,
            plan_enabled=False,
            max_rounds=5,
        ).run_with_context("运行合成计算器测试", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual("通过", turn.context.verification)
        self.assertIsNotNone(turn.context.verification_evidence)
        request_text = "\n".join(
            message.content or ""
            for request in provider.requests
            for message in request
        )
        self.assertIn("Python 3", request_text)
        self.assertIn("Ran 1 test", request_text)
        self.assertEqual(2, len(self.approvals))

    def test_legacy_fake_provider_uses_same_real_test_path(self) -> None:
        """legacy_json 不使用另一条命令或验证捷径。"""

        provider = self._legacy_provider(
            (
                (
                    "run_command",
                    {"command": "python3 -m unittest -v test_calculator"},
                ),
                ("finish", {"summary": "legacy complete"}),
            )
        )
        turn = CodingAgent(
            provider,
            self.registry,
            tool_protocol="legacy_json",
            plan_enabled=False,
            max_rounds=4,
        ).run_with_context("运行 legacy 合成测试", SessionContext())

        self.assertTrue(turn.result.ok, turn.result.summary)
        self.assertEqual("通过", turn.context.verification)
        self.assertIsNotNone(turn.context.verification_evidence)
        request_text = "\n".join(
            message.content or ""
            for request in provider.requests
            for message in request
        )
        self.assertIn("Ran 1 test", request_text)

    def test_real_failed_test_and_version_only_cannot_be_hidden_by_finish(self) -> None:
        """真实失败与仅查询版本都不能被模型的完成文案提升为成功。"""

        (self.workspace / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b + 1\n",
            encoding="utf-8",
        )
        failed_provider = self._native_provider(
            (
                (
                    "test",
                    "run_command",
                    {"command": "python3 -m unittest -v test_calculator"},
                ),
                ("finish", "finish", {"summary": "claim success"}),
            )
        )
        failed = CodingAgent(
            failed_provider,
            self.registry,
            plan_enabled=False,
            max_rounds=3,
        ).run_with_context("错误实现", SessionContext())
        self.assertFalse(failed.result.ok)
        self.assertEqual("失败", failed.context.verification)

        # 新任务先形成一次受控修改，再只查询版本并尝试完成。
        (self.workspace / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n",
            encoding="utf-8",
        )
        version_provider = self._native_provider(
            (
                (
                    "edit",
                    "edit_file",
                    {
                        "path": "calculator.py",
                        "old_text": "return a + b",
                        "new_text": "return int(a + b)",
                    },
                ),
                ("version", "run_command", {"command": "python3 --version"}),
                ("finish", "finish", {"summary": "version is enough"}),
            )
        )
        version_only = CodingAgent(
            version_provider,
            self.registry,
            plan_enabled=False,
            max_rounds=4,
        ).run_with_context("只查询版本", SessionContext())
        self.assertFalse(version_only.result.ok)
        self.assertTrue(version_only.context.verification_required)
        self.assertIsNone(version_only.context.verification_evidence)

    def test_async_registry_runs_real_version_query_with_same_semantics(self) -> None:
        """异步工具入口复用同一策略和真实进程，不产生验证或文件副作用。"""

        result = asyncio.run(
            self.registry.execute_async(
                "run_command",
                {"command": "python3 --version"},
            )
        )

        self.assertTrue(result.ok, result.output)
        self.assertIn("Python 3", result.output)
        self.assertEqual(EffectState.NONE, result.file_effects.state)
        self.assertIsNone(result.verification_passed)
        self.assertIsNone(result.verification_evidence)


if __name__ == "__main__":
    unittest.main()
