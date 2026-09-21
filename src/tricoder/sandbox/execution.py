"""与 Agent 解耦的受控执行后端。

模型只能通过 ``run_command`` 提供逻辑命令文本。命令策略完成白名单和路径校验后，
才会构造这里的不可变请求；镜像、挂载和 Docker 参数不属于请求的一部分。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol

from tricoder.core.cancellation import CancellationToken
from tricoder.models import SandboxConfig
from tricoder.policy import CommandPolicy
from tricoder.subprocess_control import (
    BoundedProcessResult,
    ProcessExecutionUncertain,
    run_bounded_process,
)


class ExecutionBackendError(OSError):
    """执行后端在启动前安全拒绝或不可用。"""


class ExecutionUncertain(ExecutionBackendError):
    """执行可能已经开始，但停止或清理结果无法确认。"""

    def __init__(self, *, cleanup_failed: bool) -> None:
        super().__init__("执行结果或资源清理无法确认")
        self.cleanup_failed = cleanup_failed


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """经过策略校验的执行请求；cwd 始终相对后端绑定工作区。"""

    argv: tuple[str, ...]
    cwd: str
    timeout: float
    max_output_bytes: int
    environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(not isinstance(item, str) or not item or "\x00" in item for item in self.argv)
        ):
            raise ValueError("argv 必须是非空且不含 NUL 的字符串元组")
        if not isinstance(self.cwd, str) or not self.cwd or "\\" in self.cwd:
            raise ValueError("cwd 必须是规范相对 POSIX 路径")
        relative = PurePosixPath(self.cwd)
        if relative.is_absolute() or ".." in relative.parts or self.cwd not in {".", relative.as_posix()}:
            raise ValueError("cwd 必须位于执行工作区内")
        if (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or self.timeout < 0
        ):
            # 0 是既有本地执行器支持的“立即超时”，异步清理边界测试也依赖
            # 这一语义；负值仍然属于无效输入。
            raise ValueError("timeout 不能为负数")
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须为正整数")
        copied: dict[str, str] = {}
        for key, value in self.environment.items():
            if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value:
                raise ValueError("environment 必须是安全字符串映射")
            copied[key] = value
        object.__setattr__(self, "environment", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """后端无关结果；容器结果额外绑定真实容器和镜像身份。"""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_exceeded: bool = False
    started: bool = False
    cleanup_confirmed: bool = True
    backend: str = "local"
    container_id: str | None = None
    image_id: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in {"local", "docker"}:
            raise ValueError("未知执行后端")
        if self.backend == "local" and (self.container_id is not None or self.image_id is not None):
            raise ValueError("local 结果不能携带容器身份")
        if self.backend == "docker" and self.started and (
            not self.container_id or not self.image_id
        ):
            raise ValueError("已启动的 Docker 结果必须绑定容器和镜像身份")


class ExecutionBackend(Protocol):
    mode: str

    def execute(
        self,
        request: ExecutionRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ExecutionResult:
        """执行请求并返回可验证的清理状态。"""


ProcessRunner = Callable[..., BoundedProcessResult]


class LocalExecutionBackend:
    """兼容既有本地模式的受管进程树后端。"""

    mode = "local"

    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: ProcessRunner = run_bounded_process,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ExecutionBackendError("执行工作区不存在")
        self._process_runner = process_runner

    def execute(
        self,
        request: ExecutionRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ExecutionResult:
        candidate = (self.workspace / Path(*PurePosixPath(request.cwd).parts)).resolve()
        if not candidate.is_relative_to(self.workspace) or not candidate.is_dir():
            raise ExecutionBackendError("命令工作目录不在执行工作区内")
        try:
            completed = self._process_runner(
                list(request.argv),
                cwd=candidate,
                env=dict(request.environment),
                timeout=request.timeout,
                max_output_bytes=request.max_output_bytes,
                cancellation=cancellation,
            )
        except ProcessExecutionUncertain as exc:
            raise ExecutionUncertain(cleanup_failed=exc.cleanup_failed) from exc
        return ExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=completed.timed_out,
            output_exceeded=completed.output_exceeded,
            started=True,
            cleanup_confirmed=not completed.cleanup_failed,
            backend=self.mode,
        )


def build_execution_backend(
    config: SandboxConfig,
    workspace: Path,
    *,
    session_id: str = "standalone",
    generation: int = 0,
    state_path: Path | None = None,
    docker_factory: Callable[..., ExecutionBackend] | None = None,
    local_factory: Callable[[Path], ExecutionBackend] = LocalExecutionBackend,
) -> ExecutionBackend:
    """按显式模式创建后端；Docker 失败绝不尝试 local。"""

    if config.mode == "local":
        return local_factory(workspace)
    if docker_factory is None:
        from tricoder.sandbox.docker import DockerExecutionBackend

        docker_factory = DockerExecutionBackend
    return docker_factory(
        workspace,
        config,
        session_id=session_id,
        generation=generation,
        state_path=state_path,
    )


_CONTAINER_EXECUTABLES = {
    "python": "/usr/local/bin/python",
    "git": "/usr/bin/git",
}


def build_command_policy(
    config: SandboxConfig,
    workspace: Path,
    *,
    local_factory: Callable[[Path], CommandPolicy] = CommandPolicy,
) -> CommandPolicy:
    """让命令语义策略与执行程序信任根使用同一模式。"""

    if config.mode == "local":
        return local_factory(workspace)

    def container_resolver(name: str) -> str:
        try:
            return _CONTAINER_EXECUTABLES[name]
        except KeyError as exc:
            raise ValueError("镜像内执行程序不在固定映射中") from exc

    return CommandPolicy(
        workspace,
        executable_resolver=container_resolver,
        allow_git=False,
    )
