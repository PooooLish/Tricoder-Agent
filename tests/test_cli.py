import io
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder.audit import AuditLogger
from tricoder.cli import ConsoleApprover, build_parser, main
from tricoder.models import (
    Message,
    ProviderConfig,
    ProviderResponse,
    ToolCall,
    ToolDefinition,
)
from tricoder.providers import create_provider
from tricoder.sessions import SessionError, SessionStore


class FinishingProvider:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.messages = list(messages)
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    "call-finish",
                    "finish",
                    {"summary": "演示任务完成"},
                ),
            ),
            finish_reason="tool_calls",
        )


class ScriptedProvider:
    """按顺序返回行动，用于覆盖 CLI 到 Agent 的状态传递。"""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.histories: list[list[Message]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.histories.append(list(messages))
        response = self.responses.pop(0)
        return ProviderResponse(
            tool_calls=(
                ToolCall(
                    f"call-{len(self.histories)}",
                    str(response["tool"]),
                    dict(response["arguments"]),  # type: ignore[arg-type]
                ),
            ),
            finish_reason="tool_calls",
        )


class LegacyFinishingProvider:
    """记录旧协议请求，确保 CLI 不会把显式回滚覆盖为 native。"""

    def __init__(self) -> None:
        self.tool_batches: list[tuple[ToolDefinition, ...]] = []

    def complete(
        self,
        _messages: list[Message],
        tools: list[ToolDefinition] | tuple[ToolDefinition, ...] = (),
    ) -> ProviderResponse:
        self.tool_batches.append(tuple(tools))
        return ProviderResponse(
            content=json.dumps(
                {
                    "tool": "finish",
                    "arguments": {"summary": "旧协议任务完成"},
                    "reason": "返回结果",
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


class CliTests(unittest.TestCase):
    def test_one_shot_run_uses_shared_provider_factory_by_default(self) -> None:
        """防止 CLI 一次性运行保留独立工厂并绕过 Provider 注册表。"""
        default_factory = inspect.signature(main).parameters["provider_factory"].default

        self.assertIs(create_provider, default_factory)

    def test_bare_and_chat_enter_same_injected_shell(self) -> None:
        """防止裸入口和 chat 走到不同的交互装配路径或创建真实 Provider。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store_path = root / "state" / "sessions.db"
            entered: list[tuple[str, Path]] = []
            database_paths: list[Path] = []
            environment = {
                "OPENAI_API_KEY": "test-key",
                "LOCALAPPDATA": str(root / "state"),
                "XDG_STATE_HOME": str(root / "state"),
            }

            def store_factory(database_path: Path) -> SessionStore:
                database_paths.append(database_path)
                return SessionStore(store_path.resolve())

            def shell_factory(runtime, _ui, *, input_fn):  # type: ignore[no-untyped-def]
                entered.append((runtime.current.record.name, runtime.current.record.workspace))
                self.assertIsNotNone(input_fn)

                class RecordingShell:
                    def run(self) -> int:
                        return 0

                return RecordingShell()

            with patch("tricoder.cli.Path.cwd", return_value=workspace):
                exit_code = main(
                    [],
                    environ=environment,
                    provider_factory=lambda _config, _timeout: FinishingProvider(),
                    session_store_factory=store_factory,
                    shell_factory=shell_factory,
                )
            self.assertEqual(0, exit_code)
            exit_code = main(
                ["chat", "--workspace", str(workspace)],
                environ=environment,
                provider_factory=lambda _config, _timeout: FinishingProvider(),
                session_store_factory=store_factory,
                shell_factory=shell_factory,
            )
            self.assertEqual(0, exit_code)

            self.assertEqual([("default", workspace.resolve())] * 2, entered)
            self.assertEqual(2, len(database_paths))

    def test_chat_accepts_every_run_option_without_task(self) -> None:
        """防止 chat 漏掉运行限制、审计或只读选项，或错误要求 task 位置参数。"""
        args = build_parser().parse_args(
            [
                "chat",
                "--provider", "glm",
                "--workspace", "D:/workspace",
                "--model", "glm-test",
                "--base-url", "https://example.test/v1",
                "--env-file", "D:/keys.env",
                "--audit-dir", "D:/audit",
                "--max-rounds", "3",
                "--max-context-chars", "100",
                "--timeout", "2.5",
                "--read-only",
                "--no-color",
            ]
        )

        self.assertEqual("chat", args.command)
        self.assertEqual("glm", args.provider)
        self.assertEqual("glm-test", args.model)
        self.assertTrue(args.read_only)

    def test_root_help_does_not_create_interactive_dependencies(self) -> None:
        """防止 --help 意外创建 SQLite、运行时、Shell 或网络 Provider。"""
        def forbidden_store(_database_path: Path) -> SessionStore:
            raise AssertionError("--help 不应创建 SessionStore")

        def forbidden_shell(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("--help 不应创建 InteractiveShell")

        with self.assertRaises(SystemExit) as captured:
            main(
                ["--help"],
                session_store_factory=forbidden_store,
                shell_factory=forbidden_shell,
            )
        self.assertEqual(0, captured.exception.code)

    def test_chat_returns_configuration_error_when_session_store_cannot_initialize(self) -> None:
        """防止损坏或不可写 SQLite 被静默忽略并回退到其他工作区。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            shell_created = False

            def broken_store(_database_path: Path) -> SessionStore:
                raise SessionError("DATABASE-SENTINEL")

            def forbidden_shell(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal shell_created
                shell_created = True
                raise AssertionError("SQLite 初始化失败后不得创建 Shell")

            exit_code = main(
                ["chat", "--workspace", directory],
                environ={"OPENAI_API_KEY": "test-key"},
                session_store_factory=broken_store,
                shell_factory=forbidden_shell,
                output=output,
            )

            self.assertEqual(2, exit_code)
            self.assertFalse(shell_created)
            self.assertIn("配置错误", output.getvalue())
            self.assertNotIn("DATABASE-SENTINEL", output.getvalue())

    def test_doctor_checks_configuration_without_creating_provider(self) -> None:
        """防止配置检查意外发送网络请求或泄露 Key。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()

            def forbidden_factory(_config: ProviderConfig, _timeout: float) -> object:
                raise AssertionError("doctor 不应创建 Provider")

            exit_code = main(
                ["doctor", "--provider", "deepseek", "--workspace", directory],
                environ={"DEEPSEEK_API_KEY": "super-secret-value"},
                provider_factory=forbidden_factory,
                output=output,
            )

            text = output.getvalue()
            self.assertEqual(0, exit_code)
            self.assertIn("DEEPSEEK_API_KEY", text)
            self.assertIn("已设置", text)
            self.assertIn("native", text)
            self.assertNotIn("super-secret-value", text)

    def test_doctor_reports_explicit_legacy_protocol_without_exposing_key(self) -> None:
        """防止 doctor 隐藏显式回滚状态，或在诊断输出中泄露凭据。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            exit_code = main(
                ["doctor", "--provider", "openai", "--workspace", directory],
                environ={
                    "OPENAI_API_KEY": "legacy-secret-value",
                    "TRICODER_TOOL_PROTOCOL": "legacy_json",
                },
                output=output,
            )

            text = output.getvalue()
            self.assertEqual(0, exit_code)
            self.assertIn("legacy_json", text)
            self.assertNotIn("legacy-secret-value", text)

    def test_doctor_explains_invalid_tool_protocol_in_chinese(self) -> None:
        """防止协议配置错误退化为堆栈、英文内部错误或含糊提示。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            exit_code = main(
                ["doctor", "--provider", "openai", "--workspace", directory],
                environ={
                    "OPENAI_API_KEY": "invalid-protocol-secret",
                    "TRICODER_TOOL_PROTOCOL": "automatic",
                },
                output=output,
            )

            text = output.getvalue()
            self.assertEqual(2, exit_code)
            self.assertIn("工具协议", text)
            self.assertIn("native", text)
            self.assertIn("legacy_json", text)
            self.assertNotIn("invalid-protocol-secret", text)

    def test_doctor_returns_configuration_error_when_key_is_missing(self) -> None:
        """防止缺少凭据时仍报告环境健康。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            exit_code = main(
                ["doctor", "--provider", "openai", "--workspace", directory],
                environ={},
                output=output,
            )

            self.assertEqual(2, exit_code)
            self.assertIn("OPENAI_API_KEY", output.getvalue())

    def test_no_color_doctor_emits_readable_text_without_ansi(self) -> None:
        """防止关闭颜色后输出丢失，或向 CI 日志写入控制字符。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            exit_code = main(
                [
                    "doctor",
                    "--provider",
                    "glm",
                    "--workspace",
                    directory,
                    "--no-color",
                ],
                environ={"ZAI_API_KEY": "test-key"},
                output=output,
            )

            text = output.getvalue()
            self.assertEqual(0, exit_code)
            self.assertIn("配置检查", text)
            self.assertIn("glm", text)
            self.assertNotIn("\x1b[", text)

    def test_doctor_accepts_env_file_outside_workspace(self) -> None:
        """防止 CLI 丢弃用户显式指定的共享本地密钥文件。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "sandbox"
            workspace.mkdir()
            env_file = root / "shared.env.local"
            env_file.write_text("DEEPSEEK_API_KEY=shared-test-key\n", encoding="utf-8")
            output = io.StringIO()

            exit_code = main(
                [
                    "doctor",
                    "--provider",
                    "deepseek",
                    "--workspace",
                    str(workspace),
                    "--env-file",
                    str(env_file),
                    "--no-color",
                ],
                environ={},
                output=output,
            )

            self.assertEqual(0, exit_code)
            self.assertIn("密钥来源", output.getvalue())
            self.assertIn("shared.env.local", output.getvalue())
            self.assertNotIn("shared-test-key", output.getvalue())

    def test_run_uses_injected_provider_and_writes_audit_trail(self) -> None:
        """防止 CLI 没有连接 Agent 主循环或遗漏运行轨迹。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            provider = FinishingProvider()
            audit_dir = Path(directory) / "audit-output"

            exit_code = main(
                [
                    "run",
                    "检查项目",
                    "--provider",
                    "glm",
                    "--workspace",
                    directory,
                    "--audit-dir",
                    str(audit_dir),
                ],
                environ={"ZAI_API_KEY": "test-key"},
                provider_factory=lambda _config, _timeout: provider,
                input_fn=lambda _prompt: "n",
                output=output,
            )

            self.assertEqual(0, exit_code)
            self.assertIn("TriCoder CLI", output.getvalue())
            self.assertIn("演示任务完成", output.getvalue())
            self.assertIn("用户任务：检查项目", provider.messages[1].content)
            logs = list(audit_dir.glob("*.jsonl"))
            self.assertEqual(1, len(logs))
            self.assertFalse((Path(directory) / "runtime").exists())

    def test_run_preserves_explicit_legacy_tool_protocol(self) -> None:
        """防止 one-shot 装配遗漏配置，并被 CodingAgent 的 native 默认值覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            provider = LegacyFinishingProvider()
            audit_dir = Path(directory) / "audit-output"

            exit_code = main(
                [
                    "run",
                    "验证旧协议回滚",
                    "--provider",
                    "openai",
                    "--workspace",
                    directory,
                    "--audit-dir",
                    str(audit_dir),
                    "--max-rounds",
                    "1",
                ],
                environ={
                    "OPENAI_API_KEY": "test-key",
                    "TRICODER_TOOL_PROTOCOL": "legacy_json",
                },
                provider_factory=lambda _config, _timeout: provider,
                output=io.StringIO(),
            )

            self.assertEqual(0, exit_code)
            self.assertEqual([()], provider.tool_batches)

    def test_run_writes_audit_log_only_to_explicit_audit_dir(self) -> None:
        """显式审计目录应承接运行日志，目标工作区不应生成 runtime。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            audit_dir = root / "audit-output"
            workspace.mkdir()
            output = io.StringIO()
            provider = FinishingProvider()

            try:
                exit_code = main(
                    [
                        "run",
                        "检查项目",
                        "--provider",
                        "glm",
                        "--workspace",
                        str(workspace),
                        "--audit-dir",
                        str(audit_dir),
                    ],
                    environ={"ZAI_API_KEY": "test-key"},
                    provider_factory=lambda _config, _timeout: provider,
                    input_fn=lambda _prompt: "n",
                    output=output,
                )
            except SystemExit:
                exit_code = 2

            self.assertEqual(0, exit_code)
            self.assertEqual(1, len(list(audit_dir.glob("*.jsonl"))))
            self.assertFalse((workspace / "runtime").exists())

    def test_read_only_run_rejects_audit_dir_inside_workspace(self) -> None:
        """只读运行拒绝工作区内审计目录，且不创建目录或日志。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            audit_dir = workspace / "audit"
            workspace.mkdir()
            output = io.StringIO()

            exit_code = main(
                [
                    "run",
                    "检查项目",
                    "--provider",
                    "glm",
                    "--workspace",
                    str(workspace),
                    "--audit-dir",
                    str(audit_dir),
                    "--read-only",
                ],
                environ={"ZAI_API_KEY": "test-key"},
                provider_factory=lambda _config, _timeout: FinishingProvider(),
                output=output,
            )

            self.assertEqual(2, exit_code)
            self.assertFalse(audit_dir.exists())
            self.assertEqual([], list(workspace.rglob("*.jsonl")))

    def test_read_only_run_allows_audit_dir_outside_workspace(self) -> None:
        """只读运行仍应写入工作区外显式审计目录。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            audit_dir = root / "audit-output"
            workspace.mkdir()
            output = io.StringIO()

            exit_code = main(
                [
                    "run",
                    "检查项目",
                    "--provider",
                    "glm",
                    "--workspace",
                    str(workspace),
                    "--audit-dir",
                    str(audit_dir),
                    "--read-only",
                ],
                environ={"ZAI_API_KEY": "test-key"},
                provider_factory=lambda _config, _timeout: FinishingProvider(),
                input_fn=lambda _prompt: "n",
                output=output,
            )

            self.assertEqual(0, exit_code)
            self.assertEqual(1, len(list(audit_dir.glob("*.jsonl"))))
            self.assertFalse((workspace / "runtime").exists())

    def test_run_returns_one_after_file_modification_without_verification(self) -> None:
        """防止 CLI 忽略 Agent 对未验证文件修改给出的失败结果。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            audit_dir = Path(directory) / "audit-output"
            provider = ScriptedProvider(
                [
                    {
                        "tool": "create_file",
                        "arguments": {"path": "created.py", "content": "value = 1\n"},
                        "reason": "创建示例文件",
                    },
                    {
                        "tool": "finish",
                        "arguments": {"summary": "已创建文件"},
                        "reason": "结束任务",
                    },
                ]
            )

            exit_code = main(
                [
                    "run",
                    "创建示例文件",
                    "--provider",
                    "glm",
                    "--workspace",
                    directory,
                    "--audit-dir",
                    str(audit_dir),
                ],
                environ={"ZAI_API_KEY": "test-key"},
                provider_factory=lambda _config, _timeout: provider,
                input_fn=lambda _prompt: "y",
                output=output,
            )

            self.assertEqual(1, exit_code)

    def test_console_approver_accepts_only_explicit_yes(self) -> None:
        """防止空输入或模糊回答意外授权写操作。"""
        decisions = iter(["", "y", "YES", "sure"])
        output = io.StringIO()
        approver = ConsoleApprover(
            input_fn=lambda _prompt: next(decisions),
            output=output,
        )

        self.assertFalse(approver("edit_file", "diff"))
        self.assertTrue(approver("edit_file", "diff"))
        self.assertTrue(approver("run_command", "command"))
        self.assertFalse(approver("run_command", "command"))
        self.assertIn("edit_file", output.getvalue())

    def test_run_passes_max_context_chars_to_agent(self) -> None:
        """防止 CLI 接受预算选项却没有让 Agent 在下一轮压缩历史。"""
        from tricoder.agent import CONTEXT_COMPACTION_NOTICE

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            audit_dir = Path(directory) / "audit-output"
            provider = ScriptedProvider(
                [
                    {
                        "tool": "read_file",
                        "arguments": {"path": "missing.py"},
                        "reason": "生成工具结果",
                    },
                    {
                        "tool": "finish",
                        "arguments": {"summary": "完成"},
                        "reason": "结束任务",
                    },
                ]
            )

            exit_code = main(
                [
                    "run",
                    "检查上下文压缩",
                    "--provider",
                    "glm",
                    "--workspace",
                    directory,
                    "--audit-dir",
                    str(audit_dir),
                    "--max-context-chars",
                    "1",
                ],
                environ={"ZAI_API_KEY": "test-key"},
                provider_factory=lambda _config, _timeout: provider,
                input_fn=lambda _prompt: "n",
                output=output,
            )

            self.assertEqual(0, exit_code)
            self.assertEqual(2, len(provider.histories))
            self.assertIn(
                Message("system", CONTEXT_COMPACTION_NOTICE),
                provider.histories[1],
            )

    def test_run_preflights_audit_file_before_provider_creation(self) -> None:
        """防止 Provider 已创建后才发现审计路径不可写。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "audit-output"
            output = io.StringIO()
            audit_ready_when_provider_created = False

            def factory(_config: ProviderConfig, _timeout: float) -> FinishingProvider:
                nonlocal audit_ready_when_provider_created
                audit_ready_when_provider_created = (
                    audit_dir.is_dir()
                    and len(list(audit_dir.glob("*.jsonl"))) == 1
                )
                return FinishingProvider()

            exit_code = main(
                [
                    "run",
                    "检查项目",
                    "--provider",
                    "glm",
                    "--workspace",
                    directory,
                    "--audit-dir",
                    str(audit_dir),
                ],
                environ={"ZAI_API_KEY": "test-key"},
                provider_factory=factory,
                output=output,
            )

            self.assertEqual(0, exit_code)
            self.assertTrue(audit_ready_when_provider_created)

    def test_run_returns_configuration_error_when_audit_preflight_fails(self) -> None:
        """审计目录不可创建时不得构造 Provider，且必须返回稳定安全错误。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            provider_created = False

            def factory(_config: ProviderConfig, _timeout: float) -> FinishingProvider:
                nonlocal provider_created
                provider_created = True
                return FinishingProvider()

            with patch.object(
                AuditLogger,
                "prepare",
                side_effect=PermissionError("DIRECTORY-SENTINEL"),
                create=True,
            ):
                exit_code = main(
                    [
                        "run",
                        "检查项目",
                        "--provider",
                        "glm",
                        "--workspace",
                        directory,
                        "--audit-dir",
                        str(Path(directory) / "audit-output"),
                    ],
                    environ={"ZAI_API_KEY": "test-key"},
                    provider_factory=factory,
                    output=output,
                )

            self.assertEqual(2, exit_code)
            self.assertFalse(provider_created)
            self.assertIn("审计日志", output.getvalue())
            self.assertNotIn("DIRECTORY-SENTINEL", output.getvalue())

    def test_run_stops_safely_when_later_audit_append_fails(self) -> None:
        """运行期间审计追加失败不得抛异常或继续报告任务成功。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            audit_dir = Path(directory) / "audit-output"

            with patch.object(
                AuditLogger,
                "log",
                side_effect=PermissionError("APPEND-SENTINEL"),
            ):
                try:
                    exit_code = main(
                        [
                            "run",
                            "检查项目",
                            "--provider",
                            "glm",
                            "--workspace",
                            directory,
                            "--audit-dir",
                            str(audit_dir),
                        ],
                        environ={"ZAI_API_KEY": "test-key"},
                        provider_factory=lambda _config, _timeout: FinishingProvider(),
                        output=output,
                    )
                except OSError as exc:
                    self.fail(f"审计追加异常未被安全处理：{exc}")

            self.assertEqual(1, exit_code)
            self.assertIn("审计日志", output.getvalue())
            self.assertNotIn("APPEND-SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
