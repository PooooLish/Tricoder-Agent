"""配置加载与优先级合并。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from tricoder.models import AppConfig, ProviderConfig

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 的开发环境兼容路径
    tomllib = None  # type: ignore[assignment]


class ConfigError(ValueError):
    """表示用户配置不完整或不安全。"""


def _is_windows() -> bool:
    """隔离平台判断，便于在不改变 pathlib 全局状态的情况下测试。"""

    return os.name == "nt"


def default_audit_dir(environ: Mapping[str, str] | None = None) -> Path:
    """返回当前平台的默认审计目录，并始终解析为绝对路径。"""

    environment = os.environ if environ is None else environ
    if _is_windows():
        state_home = environment.get("LOCALAPPDATA")
        base_dir = Path(state_home) if state_home else Path.home() / "AppData" / "Local"
        return (base_dir / "TriCoder" / "runs").expanduser().resolve()

    state_home = environment.get("XDG_STATE_HOME")
    base_dir = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return (base_dir / "tricoder" / "runs").expanduser().resolve()


_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5",
    },
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
    },
    "glm": {
        "key_env": "ZAI_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.2",
    },
}

_OFFICIAL_BASE_URLS: dict[str, set[str]] = {
    "openai": {"https://api.openai.com/v1"},
    "deepseek": {"https://api.deepseek.com"},
    "glm": {
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/api/coding/paas/v4",
    },
}


def preview_provider_models(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    model: str | None = None,
    config_path: Path | None = None,
) -> dict[str, str]:
    """解析三个 Provider 的模型名，不读取本地密钥文件或校验 API Key。"""
    resolved_workspace = Path(workspace).resolve()
    if not resolved_workspace.is_dir():
        raise ConfigError(f"工作区不存在或不是目录：{workspace}")
    process_env = os.environ if environ is None else environ
    project = _read_project_config(config_path or resolved_workspace / ".tricoder.toml")
    agent_table = project.get("agent", {})
    providers_table = project.get("providers", {})
    if not isinstance(agent_table, dict) or not isinstance(providers_table, dict):
        raise ConfigError("agent 和 providers 配置必须是表")

    previews: dict[str, str] = {}
    for provider_name, defaults in _PROVIDERS.items():
        provider_table = providers_table.get(provider_name, {})
        if not isinstance(provider_table, dict):
            raise ConfigError(f"providers.{provider_name} 必须是表")
        selected_model = (
            model
            or process_env.get("TRICODER_MODEL")
            or agent_table.get("model")
            or provider_table.get("model")
            or defaults["model"]
        )
        if not isinstance(selected_model, str) or not selected_model.strip():
            raise ConfigError("model 不能为空")
        previews[provider_name] = selected_model.strip()
    return previews


def provider_key_env(provider: str) -> str:
    """返回 Provider 对应的 API Key 环境变量名。"""

    normalized = provider.lower().strip()
    if normalized not in _PROVIDERS:
        raise ConfigError(f"不支持的 Provider：{provider}")
    return _PROVIDERS[normalized]["key_env"]


def _validate_base_url(url: str) -> None:
    """结构化验证 base_url：HTTPS、host 非空、拒绝 userinfo/query/fragment。"""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ConfigError("base_url 格式无效") from exc
    if parsed.scheme != "https":
        raise ConfigError("base_url 必须是 HTTPS 地址")
    if not parsed.hostname:
        raise ConfigError("base_url 必须包含主机")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("base_url 不能包含用户信息")
    if parsed.query or parsed.fragment:
        raise ConfigError("base_url 不能包含查询或片段")


def _read_project_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        if tomllib is not None:
            with path.open("rb") as file:
                data = tomllib.load(file)
        else:
            data = _parse_minimal_toml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取项目配置：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("项目配置的根节点必须是表")
    return data


_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_local_env(path: Path) -> dict[str, str]:
    """读取简单的 KEY=VALUE 文件，不修改进程环境，也不回显值。"""

    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取 .env.local：{exc}") from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f".env.local 第 {line_number} 行缺少等号")
        name, raw_value = (part.strip() for part in line.split("=", 1))
        if not _ENV_NAME_PATTERN.fullmatch(name):
            raise ConfigError(f".env.local 第 {line_number} 行的变量名无效")
        if name in values:
            raise ConfigError(f".env.local 第 {line_number} 行重复定义 {name}")
        if raw_value.startswith(("'", '"')):
            quote = raw_value[0]
            if len(raw_value) < 2 or raw_value[-1] != quote:
                raise ConfigError(f".env.local 第 {line_number} 行的引号不匹配")
            value = raw_value[1:-1]
        else:
            value = raw_value
        values[name] = value
    return values


def _parse_minimal_toml(text: str) -> dict[str, object]:
    """为旧版 Python 解析 MVP 使用的简单 TOML 子集。

    正式运行仍要求 Python 3.11+。此兼容解析器只接受表、字符串、整数、
    浮点数和布尔值，不尝试替代完整 TOML 标准。
    """

    root: dict[str, object] = {}
    current: dict[str, object] = root
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            names = [part.strip() for part in line[1:-1].split(".")]
            if not all(names):
                raise ValueError(f"第 {line_number} 行的表名无效")
            current = root
            for name in names:
                value = current.setdefault(name, {})
                if not isinstance(value, dict):
                    raise ValueError(f"第 {line_number} 行的表名与键冲突")
                current = value
            continue
        if "=" not in line:
            raise ValueError(f"第 {line_number} 行缺少等号")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"第 {line_number} 行的键为空")
        current[key] = _parse_toml_scalar(raw_value, line_number)
    return root


def _parse_toml_scalar(value: str, line_number: int) -> object:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if value in {"true", "false"}:
        return value == "true"
    try:
        return float(value) if "." in value else int(value)
    except ValueError as exc:
        raise ValueError(f"第 {line_number} 行包含不支持的值") from exc


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是正整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        parsed = int(value)
    else:
        raise ConfigError(f"{name} 必须是正整数")
    if parsed <= 0:
        raise ConfigError(f"{name} 必须是正整数")
    return parsed


def _positive_float(value: object, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是正数") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} 必须是正数")
    return parsed


def load_config(
    provider: str,
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    env_file: Path | None = None,
    audit_dir: Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_rounds: int | None = None,
    max_context_chars: int | None = None,
    timeout: float | None = None,
    read_only: bool = False,
    plan_enabled: bool | None = None,
) -> AppConfig:
    """按“命令行 > 环境变量 > 项目配置 > 默认值”加载配置。"""

    provider_name = provider.lower().strip()
    if provider_name not in _PROVIDERS:
        raise ConfigError(f"不支持的 Provider：{provider}")

    resolved_workspace = workspace.resolve()
    if not resolved_workspace.is_dir():
        raise ConfigError(f"工作区不存在或不是目录：{workspace}")

    process_env = os.environ if environ is None else environ
    resolved_audit_dir = (
        audit_dir.expanduser().resolve()
        if audit_dir is not None
        else default_audit_dir(process_env)
    )
    if read_only and resolved_audit_dir.is_relative_to(resolved_workspace):
        raise ConfigError("只读模式下审计目录不能位于目标工作区内")
    selected_env_file = (
        env_file.expanduser().resolve()
        if env_file is not None
        else resolved_workspace / ".env.local"
    )
    if env_file is not None and not selected_env_file.is_file():
        raise ConfigError(f"--env-file 指定的文件不存在：{selected_env_file}")
    local_env = _read_local_env(selected_env_file)
    used_env_file = selected_env_file if selected_env_file.is_file() else None
    # 进程环境用于 CI、临时调试和安全注入，优先级高于本地文件。
    env = {**local_env, **process_env}
    defaults = _PROVIDERS[provider_name]
    project = _read_project_config(config_path or resolved_workspace / ".tricoder.toml")
    agent_table = project.get("agent", {})
    providers_table = project.get("providers", {})
    if not isinstance(agent_table, dict) or not isinstance(providers_table, dict):
        raise ConfigError("agent 和 providers 配置必须是表")
    provider_table = providers_table.get(provider_name, {})
    if not isinstance(provider_table, dict):
        raise ConfigError(f"providers.{provider_name} 必须是表")

    # API Key 刻意不读取 TOML，避免凭据进入项目文件或版本控制。
    key_env = defaults["key_env"]
    process_key = process_env.get(key_env, "").strip()
    local_key = local_env.get(key_env, "").strip()
    api_key = process_key or local_key
    if not api_key:
        raise ConfigError(f"缺少环境变量 {key_env}")
    key_source = (
        "进程环境变量"
        if process_key
        else used_env_file.name if used_env_file is not None else "未配置"
    )

    selected_model = (
        model
        or env.get("TRICODER_MODEL")
        or agent_table.get("model")
        or provider_table.get("model")
        or defaults["model"]
    )
    # TRICODER_BASE_URL 只能来自显式 CLI 参数或可信进程环境；工作区 .env.local
    # 不得控制它，否则可与进程 API Key 组合把 Authorization 发送到任意 HTTPS 主机。
    explicit_url = base_url or process_env.get("TRICODER_BASE_URL")
    project_url = provider_table.get("base_url")
    if explicit_url:
        selected_url = explicit_url
    elif project_url:
        if (
            not isinstance(project_url, str)
            or project_url.rstrip("/") not in _OFFICIAL_BASE_URLS[provider_name]
        ):
            raise ConfigError(
                "项目配置中的 base_url 只能使用当前 Provider 的官方地址；"
                "自定义地址请通过命令行或环境变量显式指定"
            )
        selected_url = project_url
    else:
        selected_url = defaults["base_url"]
    rounds_value = (
        max_rounds
        if max_rounds is not None
        else env.get("TRICODER_MAX_ROUNDS", agent_table.get("max_rounds", 30))
    )
    context_chars_value = (
        max_context_chars
        if max_context_chars is not None
        else env.get(
            "TRICODER_MAX_CONTEXT_CHARS",
            agent_table.get("max_context_chars", 80_000),
        )
    )
    timeout_value = (
        timeout
        if timeout is not None
        else env.get("TRICODER_TIMEOUT", agent_table.get("timeout", 30))
    )
    tool_protocol_value = (
        env["TRICODER_TOOL_PROTOCOL"]
        if "TRICODER_TOOL_PROTOCOL" in env
        else agent_table.get("tool_protocol", "native")
    )
    plan_value = (
        plan_enabled
        if plan_enabled is not None
        else env.get("TRICODER_PLAN", agent_table.get("plan", True))
    )
    if isinstance(plan_value, str):
        plan_value = plan_value.strip().lower() not in {"0", "false", "no", "off"}
    if not isinstance(plan_value, bool):
        raise ConfigError("plan 必须是布尔值")

    if not isinstance(selected_model, str) or not selected_model.strip():
        raise ConfigError("model 不能为空")
    if isinstance(selected_url, str):
        _validate_base_url(selected_url)
    else:
        raise ConfigError("base_url 必须是字符串")
    if (
        not isinstance(tool_protocol_value, str)
        or tool_protocol_value not in {"native", "legacy_json"}
    ):
        raise ConfigError("tool_protocol 只能是 native 或 legacy_json")

    return AppConfig(
        workspace=resolved_workspace,
        provider=ProviderConfig(
            name=provider_name,
            api_key=api_key,
            base_url=selected_url.rstrip("/"),
            model=selected_model.strip(),
        ),
        max_rounds=_positive_int(rounds_value, "max_rounds"),
        max_context_chars=_positive_int(context_chars_value, "max_context_chars"),
        timeout=_positive_float(timeout_value, "timeout"),
        read_only=read_only,
        env_file=used_env_file,
        key_source=key_source,
        audit_dir=resolved_audit_dir,
        tool_protocol=tool_protocol_value,
        plan_enabled=plan_value,
    )
