"""受控 Docker CLI 执行后端。

每条命令使用一个新容器。可信宿主进程固定镜像 ID、隔离参数、唯一容器名和
工作副本挂载；模型不能提供 Docker 参数。任何清理不确定都会冻结本后端，禁止
继续复用可能仍被后台容器写入的副本。
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.models import SandboxConfig
from tricoder.policy import is_sensitive_workspace_path
from tricoder.sandbox.execution import (
    ExecutionBackendError,
    ExecutionRequest,
    ExecutionResult,
    ExecutionUncertain,
    ProcessRunner,
)
from tricoder.subprocess_control import (
    BoundedProcessResult,
    ProcessExecutionUncertain,
    run_bounded_process,
)
from tricoder.subprocess_env import filtered_subprocess_env, trusted_path_executable


_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REMOTE_ENV = frozenset(
    {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
)
_LOCAL_ENGINE_SCHEMES = ("unix://", "npipe://")
_CONTROL_TIMEOUT = 10.0
_CONTROL_OUTPUT = 256 * 1024
_MAX_ENTRIES = 10_000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_IGNORED_OUTPUT_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_STATE_VERSION = 1
_STATE_MAX_BYTES = 4096


class DockerExecutionBackend:
    """只连接本地可信 Docker engine，并完整拥有其创建的单个容器。"""

    mode = "docker"

    def __init__(
        self,
        workspace: Path,
        config: SandboxConfig,
        *,
        docker_executable: str | None = None,
        process_runner: ProcessRunner = run_bounded_process,
        source_env: Mapping[str, str] | None = None,
        session_id: str = "standalone",
        generation: int = 0,
        state_path: Path | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir() or "," in str(self.workspace):
            raise ExecutionBackendError("Docker 执行工作区无效或路径不受支持")
        if config.mode != "docker" or config.image is None:
            raise ExecutionBackendError("Docker 后端必须绑定显式镜像")
        if _SESSION_ID.fullmatch(session_id) is None:
            raise ExecutionBackendError("Docker Session 身份无效")
        if type(generation) is not int or generation < 0:
            raise ExecutionBackendError("Docker generation 无效")
        if not isinstance(poll_interval, (int, float)) or poll_interval < 0:
            raise ExecutionBackendError("Docker 轮询间隔无效")

        raw_env = dict(os.environ if source_env is None else source_env)
        if any(raw_env.get(name) for name in _REMOTE_ENV):
            raise ExecutionBackendError("拒绝远程 Docker 主机或显式上下文")
        self._env = filtered_subprocess_env(
            raw_env,
            excluded_paths=(self.workspace, Path.cwd()),
        )
        if docker_executable is None:
            try:
                docker_executable = trusted_path_executable("docker", self._env)
            except ValueError as exc:
                raise ExecutionBackendError("Docker CLI 不可用，已拒绝宿主机回退") from exc
        self.docker_executable = str(docker_executable)
        self.config = config
        self.session_id = session_id
        self.generation = generation
        self.state_path = Path(
            state_path
            if state_path is not None
            else self.workspace.parent / "container-state.json"
        ).resolve()
        if self.state_path.is_relative_to(self.workspace):
            raise ExecutionBackendError("容器状态记录不能位于挂载副本内")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._process_runner = process_runner
        self._poll_interval = float(poll_interval)
        self._lock = threading.Lock()
        self.uncertain_container_id: str | None = None
        self._engine_endpoint: str | None = None

        self._validate_local_engine()
        self.image_id = self._resolve_image(config.image)
        self._recover_owned_container()

    def execute(
        self,
        request: ExecutionRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ExecutionResult:
        """创建、启动、观察、停止并确认删除一个真实容器。"""

        with self._lock:
            if self.uncertain_container_id is not None:
                raise ExecutionUncertain(cleanup_failed=True)
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            self._measure_workspace()
            deadline = time.monotonic() + request.timeout
            container_name = (
                f"tricoder-{self.session_id[:32]}-{self.generation}-"
                f"{secrets.token_hex(8)}"
            )
            ownership_token = secrets.token_hex(24)
            container_id: str | None = None
            try:
                self._write_state(container_name, ownership_token)
                try:
                    created = self._call(
                        self._create_args(request, container_name, ownership_token),
                        timeout=_CONTROL_TIMEOUT,
                        max_output_bytes=_CONTROL_OUTPUT,
                    )
                except ProcessExecutionUncertain as exc:
                    self._reconcile_failed_create(container_name)
                    raise ExecutionUncertain(cleanup_failed=False) from exc
                if created.timed_out or created.output_exceeded or created.returncode != 0:
                    self._reconcile_failed_create(container_name)
                    raise ExecutionBackendError("Docker 容器创建失败")
                candidate = created.stdout.strip()
                if _CONTAINER_ID.fullmatch(candidate) is None:
                    self._reconcile_failed_create(container_name)
                    raise ExecutionBackendError("Docker 容器创建结果无效")
                container_id = candidate
                try:
                    self._write_state(container_id, ownership_token)
                except ExecutionBackendError as exc:
                    cleanup_ok = self._cleanup_container(container_id, running=False)
                    if not cleanup_ok:
                        self._freeze(container_id)
                    raise ExecutionUncertain(cleanup_failed=not cleanup_ok) from exc
                if cancellation is not None and cancellation.is_cancelled:
                    cleanup_ok = self._cleanup_container(container_id, running=False)
                    if not cleanup_ok:
                        self._freeze(container_id)
                    raise CancellationError("操作已取消", cleanup_failed=not cleanup_ok)

                started = self._call(
                    ("container", "start", container_id),
                    timeout=_CONTROL_TIMEOUT,
                    max_output_bytes=_CONTROL_OUTPUT,
                )
                if started.returncode != 0 or started.timed_out or started.output_exceeded:
                    cleanup_ok = self._cleanup_container(container_id, running=None)
                    if not cleanup_ok:
                        self._freeze(container_id)
                    raise ExecutionUncertain(cleanup_failed=not cleanup_ok)

                state, terminal_reason = self._observe(
                    container_id,
                    deadline=deadline,
                    cancellation=cancellation,
                )
                running = bool(state.get("Running")) if state is not None else None
                if terminal_reason is not None and running is not False:
                    if not self._stop_and_confirm(container_id):
                        self._freeze(container_id)
                        if terminal_reason == "cancelled":
                            raise CancellationError("操作已取消", cleanup_failed=True)
                        raise ExecutionUncertain(cleanup_failed=True)
                    state = self._inspect_state(container_id)
                    if state is None or state.get("Running") is not False:
                        self._freeze(container_id)
                        raise ExecutionUncertain(cleanup_failed=True)

                logs = self._call(
                    ("container", "logs", container_id),
                    timeout=_CONTROL_TIMEOUT,
                    max_output_bytes=request.max_output_bytes,
                )
                if logs.timed_out:
                    cleanup_ok = self._cleanup_container(container_id, running=False)
                    if not cleanup_ok:
                        self._freeze(container_id)
                    raise ExecutionUncertain(cleanup_failed=not cleanup_ok)
                cleanup_ok = self._cleanup_container(container_id, running=False)
                if not cleanup_ok:
                    self._freeze(container_id)
                    raise ExecutionUncertain(cleanup_failed=True)
                # 日志读取器因输出上限主动终止时，returncode 可能为空；只要真实
                # 容器已确认删除，就应把有界的 output_exceeded 交给工具层分类，
                # 而不是丢失为笼统的“结果不确定”。
                if logs.returncode != 0 and not logs.output_exceeded:
                    raise ExecutionUncertain(cleanup_failed=False)
                if terminal_reason == "cancelled":
                    raise CancellationError("操作已取消", cleanup_failed=False)
                if terminal_reason == "workspace-limit":
                    # 容器已清理，但副本可能已经部分写入；结果必须保持不确定，
                    # 不能按“启动前失败”恢复旧验证证据。
                    raise ExecutionUncertain(cleanup_failed=False)
                exit_code = state.get("ExitCode") if isinstance(state, dict) else None
                if type(exit_code) is not int:
                    raise ExecutionUncertain(cleanup_failed=False)
                return ExecutionResult(
                    returncode=exit_code,
                    stdout=logs.stdout,
                    stderr=logs.stderr,
                    timed_out=terminal_reason == "timeout",
                    output_exceeded=logs.output_exceeded,
                    started=True,
                    cleanup_confirmed=True,
                    backend="docker",
                    container_id=container_id,
                    image_id=self.image_id,
                )
            except CancellationError:
                raise
            except ExecutionUncertain:
                raise
            except ProcessExecutionUncertain as exc:
                if container_id is not None:
                    self._freeze(container_id)
                raise ExecutionUncertain(cleanup_failed=True) from exc

    def _create_args(
        self,
        request: ExecutionRequest,
        container_name: str,
        ownership_token: str,
    ) -> tuple[str, ...]:
        container_cwd = "/workspace" if request.cwd == "." else f"/workspace/{request.cwd}"
        mount = f"type=bind,src={self.workspace},dst=/workspace,rw"
        return (
            "container", "create",
            "--name", container_name,
            "--label", "tricoder.owner=tricoder-cli",
            "--label", f"tricoder.session={self.session_id}",
            "--label", f"tricoder.generation={self.generation}",
            "--label", f"tricoder.token={ownership_token}",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "128",
            "--memory", "512m",
            "--cpus", "1.0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--user", "65532:65532",
            "--workdir", container_cwd,
            "--mount", mount,
            "--log-driver", "local",
            "--log-opt", "max-size=1m",
            "--log-opt", "max-file=1",
            "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTEST_ADDOPTS=-p no:cacheprovider",
            "--entrypoint", request.argv[0],
            self.image_id,
            *request.argv[1:],
        )

    def _observe(
        self,
        container_id: str,
        *,
        deadline: float,
        cancellation: CancellationToken | None,
    ) -> tuple[dict[str, object] | None, str | None]:
        while True:
            if cancellation is not None and cancellation.is_cancelled:
                return None, "cancelled"
            if time.monotonic() >= deadline:
                return None, "timeout"
            try:
                self._measure_workspace()
            except ExecutionBackendError:
                return None, "workspace-limit"
            state = self._inspect_state(container_id)
            if state is None:
                cleanup_ok = self._cleanup_container(container_id, running=None)
                if not cleanup_ok:
                    self._freeze(container_id)
                raise ExecutionUncertain(cleanup_failed=not cleanup_ok)
            if state.get("Running") is False:
                return state, None
            if state.get("Running") is not True:
                self._freeze(container_id)
                raise ExecutionUncertain(cleanup_failed=True)
            if self._poll_interval:
                time.sleep(min(self._poll_interval, max(0.0, deadline - time.monotonic())))

    def _inspect_state(self, container_id: str) -> dict[str, object] | None:
        inspected = self._call(
            ("container", "inspect", "--format", "{{json .State}}", container_id),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if inspected.returncode != 0 or inspected.timed_out or inspected.output_exceeded:
            return None
        try:
            state = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    def _stop_and_confirm(self, container_id: str) -> bool:
        stopped = self._call(
            ("container", "stop", "--time", "1", container_id),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if stopped.returncode != 0 or stopped.timed_out or stopped.output_exceeded:
            return False
        state = self._inspect_state(container_id)
        return state is not None and state.get("Running") is False

    def _cleanup_container(self, container_id: str, *, running: bool | None) -> bool:
        if running is not False:
            state = self._inspect_state(container_id)
            if state is None:
                return False
            if state.get("Running") is True and not self._stop_and_confirm(container_id):
                return False
            if state.get("Running") not in {True, False}:
                return False
        removed = self._call(
            ("container", "rm", "--force", container_id),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if removed.returncode != 0 or removed.timed_out or removed.output_exceeded:
            return False
        return self._confirm_absent(container_id) and self._clear_state()

    def _confirm_absent(self, container_id: str) -> bool:
        status, _identity = self._lookup_container(container_id)
        return status == "absent"

    def _lookup_container(self, reference: str) -> tuple[str, str | None]:
        inspected = self._call(
            ("container", "inspect", "--format", "{{.Id}}", reference),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if inspected.returncode == 0 and not inspected.timed_out and not inspected.output_exceeded:
            identity = inspected.stdout.strip()
            if _CONTAINER_ID.fullmatch(identity) is not None:
                return "present", identity
            return "unknown", None
        error = inspected.stderr.lower()
        if "no such object" in error or "no such container" in error:
            return "absent", None
        return "unknown", None

    def _reconcile_failed_create(self, container_name: str) -> None:
        """用预生成唯一名称确认 create 失败是否留下了未知容器。"""

        status, identity = self._lookup_container(container_name)
        if status == "absent":
            if not self._clear_state():
                self._freeze(container_name)
                raise ExecutionUncertain(cleanup_failed=True)
            return
        if status == "present" and identity is not None:
            if self._cleanup_container(identity, running=None):
                return
            self._freeze(identity)
        else:
            self._freeze(container_name)
        raise ExecutionUncertain(cleanup_failed=True)

    def retry_cleanup(self) -> bool:
        """只重试当前状态记录精确拥有的资源，绝不全局枚举或 prune。"""

        with self._lock:
            reference = self.uncertain_container_id
            if reference is None:
                return True
            record = self._read_state()
            if record is None:
                return False
            status, identity = self._lookup_owned_container(
                reference,
                str(record["token"]),
            )
            if status == "absent":
                okay = self._clear_state()
            elif status == "present" and identity is not None:
                okay = self._cleanup_container(identity, running=None)
            else:
                okay = False
            if okay:
                self.uncertain_container_id = None
            return okay

    def _freeze(self, container_id: str) -> None:
        self.uncertain_container_id = container_id

    def _recover_owned_container(self) -> None:
        record = self._read_state()
        if record is None:
            return
        if (
            record["session_id"] != self.session_id
            or record["generation"] != self.generation
            or record["image_id"] != self.image_id
        ):
            raise ExecutionBackendError("容器状态记录身份不匹配，拒绝自动清理")
        reference = str(record["reference"])
        status, identity = self._lookup_owned_container(reference, str(record["token"]))
        if status == "absent":
            if not self._clear_state():
                self._freeze(reference)
                raise ExecutionBackendError("无法清理过期容器状态记录")
            return
        if status == "present" and identity is not None:
            self.uncertain_container_id = identity
            if self._cleanup_container(identity, running=None):
                self.uncertain_container_id = None
                return
        else:
            self.uncertain_container_id = reference
        raise ExecutionBackendError("遗留容器清理无法确认，已冻结执行副本")

    def _lookup_owned_container(
        self,
        reference: str,
        token: str,
    ) -> tuple[str, str | None]:
        inspected = self._call(
            ("container", "inspect", reference),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if inspected.returncode != 0 or inspected.timed_out or inspected.output_exceeded:
            error = inspected.stderr.lower()
            if "no such object" in error or "no such container" in error:
                return "absent", None
            return "unknown", None
        try:
            payload = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            return "unknown", None
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            return "unknown", None
        metadata = payload[0]
        identity = metadata.get("Id")
        config = metadata.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        expected = {
            "tricoder.owner": "tricoder-cli",
            "tricoder.session": self.session_id,
            "tricoder.generation": str(self.generation),
            "tricoder.token": token,
        }
        if (
            not isinstance(identity, str)
            or _CONTAINER_ID.fullmatch(identity) is None
            or not isinstance(labels, dict)
            or any(labels.get(name) != value for name, value in expected.items())
        ):
            return "unknown", None
        return "present", identity

    def _write_state(self, reference: str, token: str) -> None:
        payload = {
            "version": _STATE_VERSION,
            "session_id": self.session_id,
            "generation": self.generation,
            "image_id": self.image_id,
            "reference": reference,
            "token": token,
        }
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
            os.write(descriptor, data)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.state_path)
        except OSError as exc:
            raise ExecutionBackendError("无法持久化容器所有权记录") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _read_state(self) -> dict[str, object] | None:
        if not self.state_path.exists():
            return None
        try:
            metadata = self.state_path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                self.state_path.is_symlink()
                or (reparse and attributes & reparse)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _STATE_MAX_BYTES
            ):
                raise ExecutionBackendError("容器状态记录不可信")
            raw = json.loads(self.state_path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExecutionBackendError("容器状态记录无法安全读取") from exc
        required = {"version", "session_id", "generation", "image_id", "reference", "token"}
        if (
            not isinstance(raw, dict)
            or set(raw) != required
            or raw["version"] != _STATE_VERSION
            or not isinstance(raw["session_id"], str)
            or type(raw["generation"]) is not int
            or not isinstance(raw["image_id"], str)
            or not isinstance(raw["reference"], str)
            or not isinstance(raw["token"], str)
            or re.fullmatch(r"[0-9a-f]{48}", str(raw["token"])) is None
        ):
            raise ExecutionBackendError("容器状态记录格式无效")
        return raw

    def _clear_state(self) -> bool:
        try:
            self.state_path.unlink(missing_ok=True)
            return not self.state_path.exists()
        except OSError:
            return False

    def _validate_local_engine(self) -> None:
        result = self._call(
            ("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if result.returncode != 0 or result.timed_out or result.output_exceeded:
            raise ExecutionBackendError("无法确认 Docker 上下文")
        try:
            endpoint = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionBackendError("Docker 上下文返回无效") from exc
        if not isinstance(endpoint, str) or not endpoint.startswith(_LOCAL_ENGINE_SCHEMES):
            raise ExecutionBackendError("拒绝远程 Docker engine")
        # 后续每次调用显式固定到已核验的本地 endpoint，避免当前 context
        # 在镜像检查与容器创建之间被并发切换。
        self._engine_endpoint = endpoint

    def _resolve_image(self, image: str) -> str:
        result = self._call(
            ("image", "inspect", image),
            timeout=_CONTROL_TIMEOUT,
            max_output_bytes=_CONTROL_OUTPUT,
        )
        if result.returncode != 0 or result.timed_out or result.output_exceeded:
            raise ExecutionBackendError("Docker 镜像不存在；不会自动拉取或构建")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionBackendError("Docker 镜像元数据无效") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ExecutionBackendError("Docker 镜像解析结果不唯一")
        metadata = payload[0]
        image_id = metadata.get("Id")
        config = metadata.get("Config")
        if (
            not isinstance(image_id, str)
            or _IMAGE_ID.fullmatch(image_id) is None
            or metadata.get("Os") != "linux"
            or not isinstance(config, dict)
            or config.get("Volumes") is not None
        ):
            raise ExecutionBackendError("Docker 镜像不满足固定 Linux 无声明卷要求")
        return image_id

    def _call(
        self,
        arguments: tuple[str, ...],
        *,
        timeout: float,
        max_output_bytes: int,
    ) -> BoundedProcessResult:
        engine = (
            ("--host", self._engine_endpoint)
            if self._engine_endpoint is not None
            else ()
        )
        return self._process_runner(
            [self.docker_executable, *engine, *arguments],
            cwd=self.workspace,
            env=self._env,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            cancellation=None,
        )

    def _measure_workspace(self) -> int:
        """执行期间的软磁盘限额；最终发布仍需稳定快照校验。"""

        entries = 0
        total_bytes = 0
        pending = [self.workspace]
        while pending:
            directory = pending.pop()
            try:
                children = list(os.scandir(directory))
            except OSError as exc:
                raise ExecutionBackendError("无法监测 Docker 工作副本") from exc
            for child in children:
                relative = Path(child.path).relative_to(self.workspace).as_posix()
                if child.name.lower() in _IGNORED_OUTPUT_DIRS and child.is_dir(follow_symlinks=False):
                    continue
                entries += 1
                if entries > _MAX_ENTRIES:
                    raise ExecutionBackendError("Docker 工作副本条目超过软限制")
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ExecutionBackendError("无法监测 Docker 工作副本") from exc
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if child.is_symlink() or (reparse and attributes & reparse):
                    raise ExecutionBackendError("Docker 工作副本出现链接")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(child.path))
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                    raise ExecutionBackendError("Docker 工作副本出现不支持的文件")
                if is_sensitive_workspace_path(relative) or child.name.lower().startswith(".env"):
                    raise ExecutionBackendError("Docker 工作副本出现敏感路径")
                if metadata.st_size > _MAX_FILE_BYTES:
                    raise ExecutionBackendError("Docker 工作副本单文件超过软限制")
                total_bytes += metadata.st_size
                if total_bytes > _MAX_TOTAL_BYTES:
                    raise ExecutionBackendError("Docker 工作副本总量超过软限制")
        return total_bytes
