"""Docker CLI 生命周期的纯模拟安全测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.models import SandboxConfig
from tricoder.sandbox.docker import DockerExecutionBackend
from tricoder.sandbox.execution import ExecutionRequest, ExecutionUncertain
from tricoder.subprocess_control import BoundedProcessResult


class _FakeDockerRunner:
    """只模拟 Docker CLI；测试不会连接 daemon 或真实容器。"""

    def __init__(
        self,
        *,
        stay_running: bool = False,
        fail_cleanup: bool = False,
        cancel_on_create: CancellationToken | None = None,
        uncertain_create: bool = False,
        logs_output_exceeded: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stay_running = stay_running
        self.fail_cleanup = fail_cleanup
        self.container_id = "a" * 64
        self.inspect_count = 0
        self.removed = False
        self.stopped = False
        self.cancel_on_create = cancel_on_create
        self.uncertain_create = uncertain_create
        self.logs_output_exceeded = logs_output_exceeded
        self.labels: dict[str, str] = {}

    def __call__(self, args, **_kwargs):  # type: ignore[no-untyped-def]
        command = tuple(str(item) for item in args)
        self.calls.append(command)
        joined = " ".join(command)
        if "context inspect" in joined:
            return BoundedProcessResult(0, '"unix:///var/run/docker.sock"\n', "")
        if "image inspect" in joined:
            payload = [{
                "Id": "sha256:" + "b" * 64,
                "Os": "linux",
                "Config": {"Volumes": None},
            }]
            return BoundedProcessResult(0, json.dumps(payload), "")
        if " container create " in f" {joined} ":
            for index, item in enumerate(command[:-1]):
                if item == "--label" and "=" in command[index + 1]:
                    name, value = command[index + 1].split("=", 1)
                    self.labels[name] = value
            if self.cancel_on_create is not None:
                self.cancel_on_create.cancel()
            if self.uncertain_create:
                return BoundedProcessResult(None, "", "", timed_out=True)
            return BoundedProcessResult(0, self.container_id + "\n", "")
        if " container start " in f" {joined} ":
            self.stopped = False
            return BoundedProcessResult(0, self.container_id + "\n", "")
        if " container logs " in f" {joined} ":
            if self.logs_output_exceeded:
                return BoundedProcessResult(
                    None,
                    "bounded-output",
                    "",
                    output_exceeded=True,
                )
            return BoundedProcessResult(0, "sandbox-output\n", "")
        if " container stop " in f" {joined} ":
            if self.fail_cleanup:
                return BoundedProcessResult(1, "", "daemon unavailable")
            self.stay_running = False
            self.stopped = True
            return BoundedProcessResult(0, self.container_id + "\n", "")
        if " container rm " in f" {joined} ":
            if self.fail_cleanup:
                return BoundedProcessResult(1, "", "daemon unavailable")
            self.removed = True
            return BoundedProcessResult(0, self.container_id + "\n", "")
        if " container inspect " in f" {joined} ":
            if self.removed:
                return BoundedProcessResult(1, "", "Error: No such object")
            if "--format" not in command:
                payload = [{
                    "Id": self.container_id,
                    "Config": {"Labels": dict(self.labels)},
                }]
                return BoundedProcessResult(0, json.dumps(payload), "")
            if "{{.Id}}" in command:
                return BoundedProcessResult(0, self.container_id + "\n", "")
            self.inspect_count += 1
            running = False if self.stopped else (self.stay_running or self.inspect_count == 1)
            state = {
                "Running": running,
                "Status": "running" if running else "exited",
                "ExitCode": 0 if not running else 0,
                "OOMKilled": False,
            }
            return BoundedProcessResult(0, json.dumps(state), "")
        raise AssertionError(f"未预期的 Docker CLI：{joined}")


class DockerExecutionBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.config = SandboxConfig(
            mode="docker",
            image="python@sha256:" + "1" * 64,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _backend(self, runner: _FakeDockerRunner) -> DockerExecutionBackend:
        return DockerExecutionBackend(
            self.workspace,
            self.config,
            docker_executable="/trusted/docker",
            process_runner=runner,
            source_env={"PATH": "/trusted"},
            session_id="session-a",
            generation=3,
            poll_interval=0,
        )

    @staticmethod
    def _request(timeout: float = 2.0) -> ExecutionRequest:
        return ExecutionRequest(
            argv=("/usr/local/bin/python", "-m", "unittest", "-q"),
            cwd=".",
            timeout=timeout,
            max_output_bytes=1024,
            environment={"OPENAI_API_KEY": "must-not-cross", "SAFE": "also-not-forwarded"},
        )

    def test_create_uses_fixed_isolation_flags_image_id_and_only_workspace_mount(self) -> None:
        """破坏点：镜像默认值、宿主环境或额外挂载可能扩大容器权限。"""
        runner = _FakeDockerRunner()
        backend = self._backend(runner)

        result = backend.execute(self._request())

        create = next(call for call in runner.calls if "create" in call)
        joined = " ".join(create)
        for required in (
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--pids-limit 128",
            "--memory 512m",
            "--cpus 1.0",
            "--user 65532:65532",
            "--entrypoint /usr/local/bin/python",
            "tricoder.session=session-a",
            "tricoder.generation=3",
        ):
            self.assertIn(required, joined)
        self.assertIn("sha256:" + "b" * 64, create)
        self.assertNotIn(self.config.image, create)
        self.assertEqual(1, sum(item == "--mount" for item in create))
        self.assertIn(str(self.workspace), joined)
        self.assertNotIn("OPENAI_API_KEY", joined)
        self.assertNotIn("must-not-cross", joined)
        self.assertNotIn("dst=/var/run/docker.sock", joined)
        self.assertEqual("docker", result.backend)
        self.assertEqual(runner.container_id, result.container_id)
        self.assertTrue(result.cleanup_confirmed)

    def test_remote_docker_environment_is_rejected_before_cli_call(self) -> None:
        """破坏点：DOCKER_HOST 可把源码副本发送给远程 daemon。"""
        runner = _FakeDockerRunner()
        with self.assertRaisesRegex(Exception, "远程|上下文"):
            DockerExecutionBackend(
                self.workspace,
                self.config,
                docker_executable="/trusted/docker",
                process_runner=runner,
                source_env={"PATH": "/trusted", "DOCKER_HOST": "tcp://remote:2375"},
            )
        self.assertEqual([], runner.calls)

    def test_cancel_stops_inspects_removes_and_confirms_absence(self) -> None:
        """破坏点：只终止 docker 客户端会让真实容器继续后台运行。"""
        runner = _FakeDockerRunner(stay_running=True)
        token = CancellationToken()
        token.cancel()
        backend = self._backend(runner)

        with self.assertRaises(CancellationError) as raised:
            backend.execute(self._request(), cancellation=token)

        self.assertFalse(raised.exception.cleanup_failed)
        joined_calls = [" ".join(call) for call in runner.calls]
        # 预取消发生在 create 前，不产生任何容器。
        self.assertFalse(any("container create" in call for call in joined_calls))

    def test_cancel_after_create_cleans_known_container_before_raising(self) -> None:
        token = CancellationToken()
        runner = _FakeDockerRunner(cancel_on_create=token)
        backend = self._backend(runner)

        with self.assertRaises(CancellationError) as raised:
            backend.execute(self._request(), cancellation=token)

        self.assertFalse(raised.exception.cleanup_failed)
        joined_calls = [" ".join(call) for call in runner.calls]
        self.assertTrue(any("container create" in call for call in joined_calls))
        self.assertFalse(any("container start" in call for call in joined_calls))
        self.assertTrue(any("container rm" in call for call in joined_calls))

    def test_uncertain_create_is_reconciled_by_unique_name_before_return(self) -> None:
        runner = _FakeDockerRunner(uncertain_create=True)
        backend = self._backend(runner)

        with self.assertRaisesRegex(Exception, "创建失败"):
            backend.execute(self._request())

        joined_calls = [" ".join(call) for call in runner.calls]
        self.assertTrue(any("{{.Id}}" in call and "tricoder-session-a" in call for call in joined_calls))
        self.assertTrue(any("container rm" in call for call in joined_calls))
        self.assertIsNone(backend.uncertain_container_id)

    def test_timeout_cleans_actual_container_not_only_wait_client(self) -> None:
        runner = _FakeDockerRunner(stay_running=True)
        backend = self._backend(runner)

        result = backend.execute(self._request(timeout=0.0001))

        joined_calls = [" ".join(call) for call in runner.calls]
        self.assertTrue(result.timed_out)
        self.assertTrue(any("container stop" in call for call in joined_calls))
        self.assertTrue(any("container rm" in call for call in joined_calls))
        self.assertTrue(result.cleanup_confirmed)

    def test_output_flood_is_bounded_and_container_is_removed(self) -> None:
        runner = _FakeDockerRunner(logs_output_exceeded=True)
        backend = self._backend(runner)

        result = backend.execute(self._request())

        self.assertTrue(result.output_exceeded)
        self.assertEqual("bounded-output", result.stdout)
        self.assertTrue(runner.removed)
        self.assertTrue(result.cleanup_confirmed)

    def test_cleanup_uncertainty_blocks_reuse_and_preserves_container_identity(self) -> None:
        """破坏点：daemon 失联后继续复用副本会与未知后台写入并发。"""
        runner = _FakeDockerRunner(stay_running=True, fail_cleanup=True)
        backend = self._backend(runner)

        with self.assertRaises(ExecutionUncertain) as raised:
            backend.execute(self._request(timeout=0.0001))
        self.assertTrue(raised.exception.cleanup_failed)
        self.assertEqual(runner.container_id, backend.uncertain_container_id)

        calls_before = len(runner.calls)
        with self.assertRaises(ExecutionUncertain):
            backend.execute(self._request())
        self.assertEqual(calls_before, len(runner.calls))

    def test_restart_recovers_only_exact_labelled_container_from_local_record(self) -> None:
        runner = _FakeDockerRunner(stay_running=True, fail_cleanup=True)
        first = self._backend(runner)
        with self.assertRaises(ExecutionUncertain):
            first.execute(self._request(timeout=0.0001))
        self.assertTrue(first.state_path.is_file())

        runner.fail_cleanup = False
        recovered = self._backend(runner)

        self.assertIsNone(recovered.uncertain_container_id)
        self.assertFalse(recovered.state_path.exists())
        self.assertTrue(runner.removed)


if __name__ == "__main__":
    unittest.main()
