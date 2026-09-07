"""MCP stdio server 的启动校验、最小环境与强制审批边界。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from tricoder.models import MCPServerConfig
from tricoder.policy import PolicyError, WorkspacePolicy
from tricoder.subprocess_env import (
    filtered_subprocess_env,
    trusted_path_executable,
    trusted_python_executable,
)


# 这些键来自 mcp==2.1.1 的 stdio 默认环境集合。升级 SDK 时必须重新核对，
# 并由测试保证每个键都被显式覆盖，避免 SDK 再次从父进程隐式继承个人信息。
WINDOWS_SDK_DEFAULT_KEYS = frozenset(
    {
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "USERNAME",
        "USERPROFILE",
    }
)
POSIX_SDK_DEFAULT_KEYS = frozenset(
    {"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"}
)

MCP_START_APPROVAL_ACTION = "dangerous_mcp_server_start"

_COMMAND_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_WINDOWS_ABSOLUTE_PREFIX = re.compile(r"^[A-Za-z]:")
_PROVIDER_CREDENTIAL_NAMES = frozenset(
    {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ZAI_API_KEY"}
)
_MAX_ARGUMENTS = 64
_MAX_ARGUMENT_CHARS = 4_096
_MAX_ARGV_CHARS = 32_768

_WINDOWS_RETAINED_KEYS = frozenset(
    {"PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP"}
)
_POSIX_RETAINED_KEYS = frozenset({"PATH", "SHELL", "TERM"})
_WINDOWS_PERSONAL_KEYS = WINDOWS_SDK_DEFAULT_KEYS - _WINDOWS_RETAINED_KEYS
_POSIX_PERSONAL_KEYS = POSIX_SDK_DEFAULT_KEYS - _POSIX_RETAINED_KEYS
_PERSONAL_CREDENTIAL_NAMES = frozenset(
    name.upper() for name in _WINDOWS_PERSONAL_KEYS | _POSIX_PERSONAL_KEYS
)


class MCPLaunchError(RuntimeError):
    """MCP server 在创建进程前被安全边界拒绝。"""


class MCPStartRejected(MCPLaunchError):
    """用户拒绝启动一个已完成静态校验的 MCP server。"""


@dataclass(frozen=True, slots=True, init=False)
class MCPLaunchRequest:
    """已经通过启动校验、可交给 stdio client 的不可变快照。"""

    command: str
    args: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(repr=False)
    approval_detail: str

    def __init__(
        self,
        command: str,
        args: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        approval_detail: str,
    ) -> None:
        # MappingProxyType 包裹独立副本：来源 mapping 和调用方都不能在审批后篡改环境。
        frozen_env = MappingProxyType(dict(env))
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "args", tuple(args))
        object.__setattr__(self, "cwd", Path(cwd))
        object.__setattr__(self, "env", frozen_env)
        object.__setattr__(self, "approval_detail", approval_detail)


def prepare_mcp_launch(
    config: MCPServerConfig,
    *,
    workspace: Path,
    source_env: Mapping[str, str],
) -> MCPLaunchRequest:
    """在审批和进程创建前构造一个最小、不可变的 MCP 启动请求。"""

    _validate_config_state(config, source_env)
    _validate_command_name(config.command)
    arguments = _validate_arguments(config.command, config.args, workspace)
    _reject_sensitive_argv(config, arguments, source_env)
    _reject_install_signatures(config.command, arguments)

    try:
        resolved_workspace = Path(workspace).resolve(strict=True)
    except OSError as exc:
        raise MCPLaunchError("mcp_workspace_invalid") from exc
    if not resolved_workspace.is_dir():
        raise MCPLaunchError("mcp_workspace_invalid")

    command = _resolve_command(config.command, source_env)
    _reject_sensitive_argv(config, (command, *arguments), source_env)
    environment = _build_minimal_environment(config, source_env)
    approval_detail = (
        f"executable={command}; "
        f"args={json.dumps(arguments, ensure_ascii=False)}"
    )
    return MCPLaunchRequest(
        command=command,
        args=arguments,
        cwd=resolved_workspace,
        env=environment,
        approval_detail=approval_detail,
    )


def approve_mcp_start(
    request: MCPLaunchRequest,
    server_id: str,
    approver: Callable[[str, str], bool],
) -> bool:
    """把启动请求恰好交给人工审批一次；拒绝时仅暴露稳定错误类别。"""

    safe_server_id = _safe_server_id(server_id)
    detail = f"server={safe_server_id}; {request.approval_detail}"
    accepted = bool(approver(MCP_START_APPROVAL_ACTION, detail))
    if not accepted:
        raise MCPStartRejected("mcp_start_rejected")
    return True


def _validate_config_state(
    config: MCPServerConfig,
    source_env: Mapping[str, str],
) -> None:
    if not config.enabled:
        raise MCPLaunchError("mcp_server_disabled")
    if not config.credentials_authorized:
        raise MCPLaunchError("mcp_credentials_unauthorized")
    if not config.credentials_present:
        raise MCPLaunchError("mcp_credentials_missing")
    for name in config.credential_env:
        if name.upper() in _PROVIDER_CREDENTIAL_NAMES:
            raise MCPLaunchError("mcp_credentials_unauthorized")
        if name.upper() in _PERSONAL_CREDENTIAL_NAMES:
            raise MCPLaunchError("mcp_credentials_unauthorized")
        value = _environment_value(source_env, name)
        if value is None or not value.strip():
            raise MCPLaunchError("mcp_credentials_missing")


def _validate_arguments(
    command: str,
    args: tuple[str, ...],
    workspace: Path,
) -> tuple[str, ...]:
    if not isinstance(args, tuple) or len(args) > _MAX_ARGUMENTS:
        raise MCPLaunchError("mcp_arguments_invalid")
    if not all(isinstance(argument, str) for argument in args):
        raise MCPLaunchError("mcp_arguments_invalid")
    if any("\0" in argument or len(argument) > _MAX_ARGUMENT_CHARS for argument in args):
        raise MCPLaunchError("mcp_arguments_invalid")
    argv_chars = len(command) + sum(len(argument) for argument in args) + len(args)
    if argv_chars > _MAX_ARGV_CHARS:
        raise MCPLaunchError("mcp_arguments_invalid")

    try:
        policy = WorkspacePolicy(Path(workspace))
        for argument in args:
            candidate = argument.partition("=")[2] if argument.startswith("-") and "=" in argument else argument
            if _looks_like_path(candidate):
                policy.resolve_path(candidate, must_exist=False)
    except (OSError, PolicyError) as exc:
        raise MCPLaunchError("mcp_argument_path_untrusted") from exc
    return tuple(args)


def _resolve_command(command: str, source_env: Mapping[str, str]) -> str:
    _validate_command_name(command)

    normalized = command.lower()
    if normalized in {"python", "python.exe"}:
        try:
            return trusted_python_executable()
        except ValueError as exc:
            raise MCPLaunchError("mcp_executable_untrusted") from exc

    lookup_name = command
    if os.name == "nt" and normalized.endswith(".exe"):
        lookup_name = command[:-4]
    try:
        resolution_env = filtered_subprocess_env(source_env)
        return trusted_path_executable(lookup_name, resolution_env)
    except ValueError as exc:
        raise MCPLaunchError("mcp_executable_untrusted") from exc


def _validate_command_name(command: object) -> None:
    if (
        not isinstance(command, str)
        or not _COMMAND_NAME_PATTERN.fullmatch(command)
        or command in {".", ".."}
        or "/" in command
        or "\\" in command
        or _WINDOWS_ABSOLUTE_PREFIX.match(command)
    ):
        raise MCPLaunchError("mcp_executable_untrusted")


def _reject_install_signatures(command: str, args: tuple[str, ...]) -> None:
    executable = command.lower().removesuffix(".exe")
    lowered = tuple(argument.lower() for argument in args)
    rejected = (
        executable == "npx" and _contains_option(lowered, "--yes", "-y")
    ) or (
        executable == "npm"
        and _contains_word(lowered, "exec")
        and _contains_option(lowered, "--yes", "-y")
    ) or (
        executable == "pip" and _contains_word(lowered, "install")
    ) or (
        executable == "python"
        and _contains_python_pip_install(args)
    ) or (
        executable == "uv"
        and _contains_word(lowered, "run")
        and _contains_option(lowered, "--with")
    )
    if rejected:
        raise MCPLaunchError("mcp_runtime_install_rejected")


def _contains_option(args: tuple[str, ...], *names: str) -> bool:
    """识别 `--flag` 与 `--flag=value`，不依赖全局选项所在索引。"""

    return any(
        token == name or token.startswith(f"{name}=")
        for token in args
        for name in names
    )


def _contains_word(args: tuple[str, ...], word: str) -> bool:
    """保守识别安装器子命令；出现同名位置项即视为危险签名。"""

    return word in args


def _contains_python_pip_install(args: tuple[str, ...]) -> bool:
    """只解析解释器选项；遇到脚本、-c 或首个 -m 后不再扫描入口。"""

    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--check-hash-based-pycs":
            index += 1
            continue
        if token.startswith("--check-hash-based-pycs="):
            continue
        if token == "-" or not token.startswith("-") or token.startswith("--"):
            return False
        for offset, option in enumerate(token[1:], start=1):
            if option == "m":
                module = token[offset + 1 :]
                if not module:
                    if index == len(args):
                        return False
                    module = args[index]
                    index += 1
                return module == "pip" and "install" in args[index:]
            if option in "WX":
                # -W/-X 的剩余字符或下一项都是选项值，不能当作 -m 解析。
                if offset + 1 == len(token):
                    index += 1
                break
            if option not in "bBdEiIOPqRsSuvx":
                # -c、帮助/版本或未知选项均不会继续选择模块入口。
                return False
    return False


def _reject_sensitive_argv(
    config: MCPServerConfig,
    args: tuple[str, ...],
    source_env: Mapping[str, str],
) -> None:
    """拒绝把凭据值重新放进 argv，避免审批详情和进程列表泄漏。"""

    sensitive_names = set(config.credential_env) | set(_PROVIDER_CREDENTIAL_NAMES)
    sensitive_values = {
        value
        for name in sensitive_names
        if (value := _environment_value(source_env, name)) is not None
        and bool(value.strip())
    }
    if any(value in argument for argument in args for value in sensitive_values):
        raise MCPLaunchError("mcp_argv_contains_credential")


def _build_minimal_environment(
    config: MCPServerConfig,
    source_env: Mapping[str, str],
) -> dict[str, str]:
    defaults = WINDOWS_SDK_DEFAULT_KEYS if os.name == "nt" else POSIX_SDK_DEFAULT_KEYS
    retained = _WINDOWS_RETAINED_KEYS if os.name == "nt" else _POSIX_RETAINED_KEYS
    environment = {
        name: (_environment_value(source_env, name) or "") if name in retained else ""
        for name in defaults
    }
    for name in config.credential_env:
        value = _environment_value(source_env, name)
        # _validate_config_state 已保证凭据存在；这里保持 fail-closed，避免未来调用顺序改变。
        if value is None or not value.strip():
            raise MCPLaunchError("mcp_credentials_missing")
        environment[name] = value
    return environment


def _environment_value(source_env: Mapping[str, str], name: str) -> str | None:
    value = source_env.get(name)
    if value is not None or os.name != "nt":
        return value
    folded = name.casefold()
    for candidate_name, candidate_value in source_env.items():
        if candidate_name.casefold() == folded:
            return candidate_value
    return None


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    if (
        value in {".", ".."}
        or value.startswith((".", "~", "/", "\\"))
        or "/" in value
        or "\\" in value
        or _WINDOWS_ABSOLUTE_PREFIX.match(value)
    ):
        return True
    return bool(Path(value).suffix)


def _safe_server_id(server_id: str) -> str:
    if (
        not isinstance(server_id, str)
        or not server_id
        or len(server_id) > 64
        or any(character in server_id for character in "\0\r\n")
    ):
        raise MCPLaunchError("mcp_server_id_invalid")
    return server_id
