"""SessionRuntime 对 Docker 副本、发布和重启恢复的集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tricoder.models import AppConfig, ProviderConfig, SandboxConfig
from tricoder.sandbox.execution import ExecutionResult
from tricoder.session_runtime import RuntimeOptions, SessionRuntime, SessionRuntimeError
from tricoder.sessions import SessionStore


class _Backend:
    mode = "docker"
    image_id = "sha256:" + "b" * 64
    uncertain_container_id = None

    def execute(self, _request, *, cancellation=None):  # type: ignore[no-untyped-def]
        return ExecutionResult(
            0,
            "",
            "",
            started=True,
            backend="docker",
            container_id="a" * 64,
            image_id=self.image_id,
        )


class _Agent:
    pass


class SandboxRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.state = self.root / "state"
        self.store = SessionStore(self.state / "sessions.db")
        self.backend_calls: list[tuple[Path, str, int]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _config(self, **kwargs):  # type: ignore[no-untyped-def]
        return AppConfig(
            workspace=Path(kwargs["workspace"]).resolve(),
            provider=ProviderConfig("openai", "synthetic", "https://example.test", "test"),
            audit_dir=self.state / "audit",
            read_only=bool(kwargs.get("read_only", False)),
            sandbox=SandboxConfig(
                mode="docker",
                image="python@sha256:" + "1" * 64,
            ),
        )

    def _backend(
        self,
        _config,
        workspace: Path,
        *,
        session_id: str,
        generation: int,
        state_path: Path | None = None,
    ):  # type: ignore[no-untyped-def]
        self.assertIsNotNone(state_path)
        self.backend_calls.append((Path(workspace), session_id, generation))
        return _Backend()

    def _runtime(
        self,
        store: SessionStore | None = None,
        *,
        read_only: bool = False,
    ) -> SessionRuntime:
        return SessionRuntime(
            store or self.store,
            self.workspace,
            options=RuntimeOptions(
                environ={},
                sandbox_mode="docker",
                read_only=read_only,
            ),
            config_loader=self._config,
            provider_factory=lambda *_args: object(),
            agent_factory=lambda *_args, **_kwargs: _Agent(),
            approver=lambda _action, _detail: True,
            execution_backend_factory=self._backend,
        )

    def test_file_tools_and_backend_share_copy_then_confirmed_publish_updates_original(self) -> None:
        runtime = self._runtime()
        active = runtime.current
        self.assertIsNotNone(active.sandbox_workspace)
        copy = active.sandbox_workspace.execution_workspace  # type: ignore[union-attr]
        self.assertEqual(copy, active.tools.context.workspace_policy.workspace)  # type: ignore[union-attr]
        self.assertEqual(copy, self.backend_calls[-1][0])
        self.assertEqual(active.record.id, self.backend_calls[-1][1])

        read = active.tools.execute("read_file", {"path": "app.py"})  # type: ignore[union-attr]
        self.assertTrue(read.ok, read.output)
        (copy / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text("utf-8"))
        self.assertIn("M app.py", active.tools.execute("git_diff", {}).output)  # type: ignore[union-attr]

        preview = runtime.preview_sandbox_publish()
        result = runtime.apply_sandbox_publish(preview)

        self.assertTrue(result.ok)
        self.assertEqual("value = 2\n", (self.workspace / "app.py").read_text("utf-8"))
        self.assertIn("待发布文件：0", runtime.render_sandbox_status())

    def test_restart_resumes_unpublished_copy_without_restoring_trust(self) -> None:
        first = self._runtime()
        copy = first.current.sandbox_workspace.execution_workspace  # type: ignore[union-attr]
        (copy / "app.py").write_text("draft = True\n", encoding="utf-8")
        session_id = first.current.record.id

        restarted = self._runtime(SessionStore(self.state / "sessions.db"))

        self.assertEqual(session_id, restarted.current.record.id)
        self.assertTrue(restarted.current.sandbox_workspace.resumed)  # type: ignore[union-attr]
        self.assertEqual("draft = True\n", (restarted.current.sandbox_workspace.execution_workspace / "app.py").read_text("utf-8"))  # type: ignore[union-attr]
        self.assertIsNone(restarted.current.context.verification_evidence)

    def test_clear_memory_keeps_unpublished_code_copy(self) -> None:
        runtime = self._runtime()
        copy = runtime.current.sandbox_workspace.execution_workspace  # type: ignore[union-attr]
        (copy / "app.py").write_text("draft = True\n", encoding="utf-8")

        runtime.clear_current(confirmed=True)

        self.assertTrue(copy.is_dir())
        self.assertEqual("draft = True\n", (copy / "app.py").read_text("utf-8"))

    def test_read_only_session_may_preview_but_cannot_publish(self) -> None:
        runtime = self._runtime(read_only=True)
        copy = runtime.current.sandbox_workspace.execution_workspace  # type: ignore[union-attr]
        (copy / "app.py").write_text("draft = True\n", encoding="utf-8")

        preview = runtime.preview_sandbox_publish()
        with self.assertRaisesRegex(SessionRuntimeError, "只读模式"):
            runtime.apply_sandbox_publish(preview)

        self.assertEqual("value = 1\n", (self.workspace / "app.py").read_text("utf-8"))

    def test_session_database_inside_project_is_rejected_before_copy(self) -> None:
        inside_store = SessionStore(self.workspace / "sessions.db")

        # SessionStore 的既有初始化边界已在 Docker 副本创建前统一拒绝此布局。
        with self.assertRaises(SessionRuntimeError):
            self._runtime(inside_store)

        self.assertFalse((self.workspace / "runtime" / "docker-sandbox").exists())


if __name__ == "__main__":
    unittest.main()
