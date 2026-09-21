"""执行后端抽象与显式沙箱配置的契约测试。"""

from __future__ import annotations

import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

from tricoder.cli import build_parser, main
from tricoder.config import ConfigError, load_config
from tricoder.models import RunResult, SandboxConfig
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.sandbox.execution import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionBackendError,
    LocalExecutionBackend,
    build_execution_backend,
)
from tricoder.subprocess_control import BoundedProcessResult
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.verification import VerificationScope


class _RecordingBackend:
    mode = "docker"

    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest, *, cancellation=None) -> ExecutionResult:  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return ExecutionResult(
            returncode=0,
            stdout="ok\n",
            stderr="",
            started=True,
            cleanup_confirmed=True,
            backend="docker",
            image_id="sha256:synthetic",
            container_id="container-synthetic",
        )


class SandboxExecutionTests(unittest.TestCase):
    def test_execution_request_allows_immediate_timeout_but_rejects_negative(self) -> None:
        """兼容边界：0 表示立即超时，负值才是无效的时间预算。"""
        request = ExecutionRequest(
            argv=("python",),
            cwd=".",
            timeout=0,
            max_output_bytes=1,
        )

        self.assertEqual(0, request.timeout)
        with self.assertRaisesRegex(ValueError, "不能为负数"):
            ExecutionRequest(
                argv=("python",),
                cwd=".",
                timeout=-1,
                max_output_bytes=1,
            )

    def test_local_backend_preserves_relative_cwd_and_bounded_result(self) -> None:
        """破坏点：后端若可接受绝对 cwd，调用者可绕过工作区绑定。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "src").mkdir()
            captured: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

            def runner(args, *, cwd, env, timeout, max_output_bytes, cancellation):  # type: ignore[no-untyped-def]
                captured.append((tuple(args), cwd, dict(env)))
                self.assertEqual(3.0, timeout)
                self.assertEqual(1234, max_output_bytes)
                self.assertIsNone(cancellation)
                return BoundedProcessResult(0, "stdout", "stderr")

            backend = LocalExecutionBackend(workspace, process_runner=runner)
            result = backend.execute(
                ExecutionRequest(
                    argv=("python", "-m", "unittest"),
                    cwd="src",
                    timeout=3.0,
                    max_output_bytes=1234,
                    environment={"SAFE": "1"},
                )
            )

        self.assertEqual(
            [(('python', '-m', 'unittest'), workspace / "src", {"SAFE": "1"})],
            captured,
        )
        self.assertEqual(0, result.returncode)
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual("local", result.backend)
        self.assertIsNone(result.container_id)

    def test_command_policy_uses_trusted_executable_resolver(self) -> None:
        """破坏点：容器模式若仍调用宿主机 which，会绑定错误的信任根。"""
        resolved: list[str] = []

        def resolver(name: str) -> str:
            resolved.append(name)
            return f"/trusted/bin/{name}"

        policy = CommandPolicy(executable_resolver=resolver)
        args = policy.validate("python -m unittest -q")

        self.assertEqual(["python"], resolved)
        self.assertEqual("/trusted/bin/python", args[0])

    def test_run_command_dispatches_only_validated_request_to_selected_backend(self) -> None:
        """破坏点：工具若绕过后端，Docker 选择不会覆盖真实命令入口。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            backend = _RecordingBackend()
            policy = CommandPolicy(
                workspace,
                executable_resolver=lambda name: f"/opt/tricoder/bin/{name}",
            )
            registry = ToolRegistry(
                ToolContext(
                    workspace_policy=WorkspacePolicy(workspace),
                    command_policy=policy,
                    approver=lambda _action, _detail: True,
                    execution_backend=backend,
                )
            )

            result = registry.execute(
                "run_command",
                {"command": "python -m unittest -q", "cwd": "."},
            )

        self.assertTrue(result.ok, result.output)
        self.assertEqual(1, len(backend.requests))
        request = backend.requests[0]
        self.assertEqual(".", request.cwd)
        self.assertEqual("/opt/tricoder/bin/python", request.argv[0])
        self.assertNotIn("OPENAI_API_KEY", request.environment)

    def test_docker_verification_evidence_binds_session_image_container_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "test_ok.py").write_text(
                "import unittest\n",
                encoding="utf-8",
            )
            backend = _RecordingBackend()
            registry = ToolRegistry(
                ToolContext(
                    workspace_policy=WorkspacePolicy(workspace),
                    command_policy=CommandPolicy(
                        workspace,
                        executable_resolver=lambda name: f"/usr/local/bin/{name}",
                    ),
                    approver=lambda _action, _detail: True,
                    execution_backend=backend,
                    verification_scope=VerificationScope(
                        sandbox_session_id="session-a",
                        sandbox_generation=7,
                    ),
                )
            )

            result = registry.execute(
                "run_command",
                {"command": "python -m unittest -q"},
            )

        self.assertTrue(result.ok, result.output)
        self.assertIsNotNone(result.verification_evidence)
        binding = result.verification_evidence.execution  # type: ignore[union-attr]
        self.assertIsNotNone(binding)
        self.assertEqual("session-a", binding.session_id)  # type: ignore[union-attr]
        self.assertEqual(7, binding.generation)  # type: ignore[union-attr]
        self.assertEqual("sha256:synthetic", binding.image_id)  # type: ignore[union-attr]
        self.assertEqual("container-synthetic", binding.container_id)  # type: ignore[union-attr]
        self.assertTrue(binding.cleanup_confirmed)  # type: ignore[union-attr]

    def test_backend_factory_never_falls_back_when_docker_construction_fails(self) -> None:
        """破坏点：Docker 不可用时回落 local 会在宿主机执行项目代码。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            local_calls: list[str] = []

            def failing_docker(_workspace: Path, _config: SandboxConfig, **_kwargs):
                raise RuntimeError("docker unavailable")

            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                build_execution_backend(
                    SandboxConfig(mode="docker", image="python@sha256:" + "1" * 64),
                    workspace,
                    docker_factory=failing_docker,
                    local_factory=lambda _workspace: local_calls.append("local"),  # type: ignore[arg-type]
                )

        self.assertEqual([], local_calls)

    def test_sandbox_config_defaults_local_and_requires_explicit_docker_image(self) -> None:
        """破坏点：默认 Docker 或隐式镜像会改变兼容性和供应链边界。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            local = load_config(
                provider="openai",
                workspace=workspace,
                environ={"OPENAI_API_KEY": "synthetic"},
            )
            docker = load_config(
                provider="openai",
                workspace=workspace,
                environ={"OPENAI_API_KEY": "synthetic"},
                sandbox_mode="docker",
                sandbox_image="python@sha256:" + "2" * 64,
            )
            with self.assertRaises(ConfigError):
                load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={"OPENAI_API_KEY": "synthetic"},
                    sandbox_mode="docker",
                )

        self.assertEqual(SandboxConfig(), local.sandbox)
        self.assertEqual("docker", docker.sandbox.mode)
        self.assertTrue(docker.sandbox.image.endswith("2" * 64))

    def test_docker_mode_rejects_enabled_extension_families_before_startup(self) -> None:
        """破坏点：仅隐藏扩展工具不能阻止 MCP 子进程在宿主机启动。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                '[extensions]\nenabled = true\n'
                '[mcp]\nenabled = true\n'
                '[[mcp.servers]]\nid = "local"\ntransport = "stdio"\n'
                'command = "python"\nenabled = true\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "Docker.*MCP"):
                load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={"OPENAI_API_KEY": "synthetic"},
                    sandbox_mode="docker",
                    sandbox_image="python@sha256:" + "3" * 64,
                )

    def test_docker_mode_rejects_audit_directory_inside_project(self) -> None:
        """活动审计文件名称不固定，不能依赖副本按名称猜测排除。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            audit = workspace / "custom-audit"

            with self.assertRaisesRegex(ConfigError, "审计目录"):
                load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={"OPENAI_API_KEY": "synthetic"},
                    audit_dir=audit,
                    sandbox_mode="docker",
                    sandbox_image="python@sha256:" + "3" * 64,
                )

    def test_all_public_entries_parse_the_same_explicit_sandbox_options(self) -> None:
        """破坏点：漏接任一入口会让相同参数在某些模式静默使用 local。"""
        parser = build_parser()
        image = "python@sha256:" + "4" * 64
        commands = (
            ["doctor", "--sandbox", "docker", "--docker-image", image],
            ["run", "task", "--sandbox", "docker", "--docker-image", image],
            ["chat", "--sandbox", "docker", "--docker-image", image],
            ["tui", "--sandbox", "docker", "--docker-image", image],
            ["eval", "suite", "--sandbox", "docker", "--docker-image", image],
        )
        for argv in commands:
            with self.subTest(command=argv[0]):
                parsed = parser.parse_args(argv)
                self.assertEqual("docker", parsed.sandbox)
                self.assertEqual(image, parsed.docker_image)

    def test_one_shot_docker_backend_failure_returns_configuration_error_without_provider(self) -> None:
        """破坏点：后端准备失败后不得构建 Provider 或执行宿主机命令。"""
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            provider_calls: list[str] = []
            image = "python@sha256:" + "5" * 64
            with patch(
                "tricoder.cli.build_execution_backend",
                side_effect=ExecutionBackendError("synthetic-unavailable"),
            ):
                code = main(
                    [
                        "run",
                        "task",
                        "--workspace",
                        directory,
                        "--sandbox",
                        "docker",
                        "--docker-image",
                        image,
                        "--no-color",
                    ],
                    environ={"OPENAI_API_KEY": "synthetic"},
                    provider_factory=lambda *_args: provider_calls.append("provider"),  # type: ignore[arg-type]
                    output=output,
                )

        self.assertEqual(2, code)
        self.assertEqual([], provider_calls)
        self.assertIn("未回退", output.getvalue())

    def test_one_shot_docker_requires_separate_publish_confirmation(self) -> None:
        class EditingAgent:
            def __init__(self, _provider, tools, **_kwargs):  # type: ignore[no-untyped-def]
                self.tools = tools

            def run(self, _task, **_kwargs):  # type: ignore[no-untyped-def]
                target = self.tools.context.workspace_policy.workspace / "app.py"
                target.write_text("value = 2\n", encoding="utf-8")
                return RunResult(True, "done", 1, modified_files=("app.py",))

        image = "python@sha256:" + "7" * 64
        for answer, expected_code, expected_text in (
            ("no", 1, "value = 1\n"),
            ("yes", 0, "value = 2\n"),
        ):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "project"
                workspace.mkdir()
                (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
                output = io.StringIO()
                with (
                    patch("tricoder.cli.CodingAgent", EditingAgent),
                    patch(
                        "tricoder.cli.build_execution_backend",
                        side_effect=lambda *_args, **_kwargs: _RecordingBackend(),
                    ),
                ):
                    code = main(
                        [
                            "run", "edit", "--workspace", str(workspace),
                            "--audit-dir", str(root / "audit"),
                            "--sandbox", "docker", "--docker-image", image,
                            "--no-color",
                        ],
                        environ={"OPENAI_API_KEY": "synthetic"},
                        provider_factory=lambda *_args: object(),  # type: ignore[arg-type]
                        input_fn=lambda _prompt, value=answer: value,
                        output=output,
                    )

                self.assertEqual(expected_code, code)
                self.assertEqual(expected_text, (workspace / "app.py").read_text("utf-8"))
                self.assertIn("待写回原项目", output.getvalue())


if __name__ == "__main__":
    unittest.main()
