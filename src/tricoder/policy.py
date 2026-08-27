"""工作区路径与命令执行安全策略。"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

from tricoder.subprocess_env import (
    filtered_subprocess_env,
    trusted_path_executable,
    trusted_python_executable,
)


_SENSITIVE_PATH_PARTS = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".config",
        ".local",
        ".env",
        ".env.local",
        "credentials",
        "secrets",
        "id_rsa",
        "id_ed25519",
        "service-account",
        "serviceaccount",
    }
)
_SENSITIVE_PATH_PREFIXES = (
    ".env.",
    "credentials.",
    "secrets.",
    "id_rsa",
    "id_ed25519",
)
_SENSITIVE_PATH_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".gpg"})


def is_sensitive_workspace_path(path: str | Path) -> bool:
    """Return whether any path segment is reserved for credentials or secrets."""

    return any(
        _is_sensitive_path_part(part.lower())
        for part in re.split(r"[\\/]+", os.fspath(path))
        if part
    )


def _is_sensitive_path_part(lowered: str) -> bool:
    if lowered == ".env.example":
        return False
    return (
        lowered in _SENSITIVE_PATH_PARTS
        or lowered.startswith(".env")
        or any(lowered.startswith(prefix) for prefix in _SENSITIVE_PATH_PREFIXES)
        or lowered.startswith("service-account")
        or lowered.startswith("serviceaccount")
        or Path(lowered).suffix in _SENSITIVE_PATH_SUFFIXES
    )


class PolicyError(PermissionError):
    """表示动作超出了 MVP 允许的安全边界。"""


class WorkspacePolicy:
    """确保模型只能访问明确指定的工作区。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise PolicyError("工作区必须是目录")

    def resolve_path(self, path: str | Path, *, must_exist: bool = True) -> Path:
        """解析路径并同时阻止目录穿越、链接逃逸和敏感文件访问。"""

        candidate = Path(path)
        raw_parts = candidate.parts
        self._check_sensitive_parts(raw_parts)

        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        # strict=False 仍会解析路径中已经存在的符号链接，因此也能保护新文件。
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.workspace):
            raise PolicyError("目标路径超出工作区")
        self._check_sensitive_parts(resolved.relative_to(self.workspace).parts)
        if must_exist and not resolved.exists():
            raise PolicyError(f"目标路径不存在：{path}")
        return resolved

    def _check_sensitive_parts(self, parts: tuple[str, ...]) -> None:
        for part in parts:
            lowered = part.lower()
            if _is_sensitive_path_part(lowered):
                raise PolicyError(f"拒绝访问敏感路径：{part}")


class CommandPolicy:
    """只允许不改变依赖或 Git 状态的测试与检查命令。

    白名单按工具维护明确允许的参数集合；可执行程序必须是 PATH 中的纯名称，
    校验通过后解析为可信绝对路径交给 subprocess，审批与审计均展示实际程序。

    当传入 ``workspace`` 时，所有路径参数都会通过 WorkspacePolicy 真实解析
    （符号链接、junction、存在性与敏感段），防止越界读取。
    """

    _META_PATTERN = re.compile(r"[|&;><`\r\n]")
    _PYTHON_MODULES = {"unittest", "pytest", "compileall", "ruff", "mypy"}
    # 这些工具必须通过 `python -m` 运行：直接调用会被 Windows 从 cwd 或 PATH
    # 命中同名程序，无法保证执行来源可信。
    _DISALLOWED_DIRECT_TOOLS = {
        "pytest", "pytest.exe", "ruff", "ruff.exe", "mypy", "mypy.exe",
    }
    _GIT_READ_ONLY = {"status", "diff", "show", "log"}
    # Git 全局选项白名单；其余全局选项（-C、-c、--git-dir、--work-tree、
    # --exec-path 等）可切换目录、覆盖配置或执行外部程序，一律拒绝。
    _GIT_ALLOWED_PREFIX = {"--no-pager"}
    # 可能写文件、执行外部程序、配置覆盖或越界读取的 git 选项，一律拒绝。
    _GIT_FORBIDDEN = {
        "--no-index", "--ext-diff", "--textconv", "--paginate", "--output",
        "--git-dir", "--work-tree", "--exec-path", "-C", "-c",
    }
    # 每个 git 只读子命令的明确允许参数集合。
    _GIT_SUBCOMMAND_OPTIONS: dict[str, set[str]] = {
        "status": {
            "--short", "--porcelain", "--branch", "--untracked-files",
            "--ignored", "-v", "--verbose", "--no-renames",
        },
        "diff": {
            "--stat", "--numstat", "--shortstat", "--name-only", "--name-status",
            "--check", "--color", "--no-color", "--no-renames", "--exit-code",
            "--quiet", "--patch", "-p", "--no-ext-diff", "--no-textconv",
            "--no-color-moved", "--color-moved",
        },
        "show": {
            "--stat", "--numstat", "--shortstat", "--name-only", "--name-status",
            "--color", "--no-color", "--no-renames", "--format", "--oneline",
            "--quiet", "--no-ext-diff", "--no-textconv",
        },
        "log": {
            "--oneline", "--stat", "--numstat", "--shortstat", "--name-only",
            "--name-status", "-n", "--max-count", "--since", "--until",
            "--grep", "--author", "--color", "--no-color", "--format",
            "--decorate", "--no-decorate", "--no-patch", "-p", "--patch",
        },
    }
    # 每工具独立的允许参数集合；不在集合内的选项一律拒绝，
    # 避免通用黑名单随工具版本演化而漏禁。
    _UNITTEST_ALLOWED_OPTIONS = {
        "-v", "--verbose", "-q", "--quiet", "-f", "--failfast",
        "-c", "--catch", "-b", "--buffer", "-k", "--locals",
        "-s", "--start-directory", "-t", "--top-level-directory", "-p", "--pattern",
    }
    _UNITTEST_VALUE_OPTIONS = {
        "-k", "-s", "--start-directory", "-t", "--top-level-directory",
        "-p", "--pattern",
    }
    _UNITTEST_PATH_OPTIONS = {
        "-s", "--start-directory", "-t", "--top-level-directory",
    }
    _COMPILEALL_ALLOWED_OPTIONS = {
        "-q", "--quiet", "-l", "-f", "-j", "-x", "-r",
        "--invalidation-mode",
    }
    _PYTEST_ALLOWED_OPTIONS = {
        "-q", "--quiet", "-v", "--verbose", "-s", "--capture",
        "-x", "--exitfirst", "--maxfail", "-k", "-m", "--tb",
        "-r", "--durations", "--durations-min", "-l", "--showlocals",
        "--lf", "--last-failed", "--ff", "--failed-first",
        "--no-header", "--no-summary", "--collect-only", "--co",
        "--ignore", "--deselect", "--ignore-glob",
        "--import-mode", "--strict-markers",
    }
    _RUFF_ALLOWED_OPTIONS = {
        "--select", "--ignore", "--extend-select", "--extend-ignore",
        "--per-file-ignores", "--no-cache", "-q", "--quiet", "-v", "--verbose",
        "--diff", "--exit-zero", "--exit-non-zero-on-fix", "--no-fix",
        "--unsafe-fixes", "--output-format", "--no-header", "--show-fixes",
        "--statistics", "--preview", "--no-preview",
        "--target-version", "--line-length",
    }
    _MYPY_ALLOWED_OPTIONS = {
        "--ignore-missing-imports", "--no-incremental", "--exclude",
        "--follow-imports", "--python-version", "--platform",
        "--strict", "--no-strict-optional", "--warn-unused-ignores",
        "--warn-redundant-casts", "--warn-return-any",
        "--allow-untyped-defs", "--disallow-untyped-defs",
        "--show-error-codes", "--pretty", "--no-error-summary",
    }
    _ABSOLUTE_PATH_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace_policy = (
            WorkspacePolicy(workspace) if workspace is not None else None
        )
        excluded_paths = [Path.cwd().resolve(strict=False)]
        if self._workspace_policy is not None:
            excluded_paths.append(self._workspace_policy.workspace)
        self._subprocess_env = filtered_subprocess_env(
            environ,
            excluded_paths=tuple(excluded_paths),
        )

    def validate(self, command: str) -> list[str]:
        """返回可交给 `subprocess` 的参数数组，否则抛出策略错误。

        ``args[0]`` 解析为可信可执行程序的绝对路径，审批与执行均使用该值。
        """

        if not command.strip():
            raise PolicyError("命令不能为空")
        if self._META_PATTERN.search(command):
            raise PolicyError("命令包含不允许的 Shell 元字符")
        try:
            args = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            raise PolicyError(f"命令格式无效：{exc}") from exc
        if not args:
            raise PolicyError("命令不能为空")

        raw_executable = args[0]
        executable = Path(raw_executable).name.lower()
        if self._is_qualified_path(raw_executable):
            raise PolicyError(f"可执行程序必须是 PATH 中的纯名称：{raw_executable}")
        if executable in {"python", "python.exe", "py", "py.exe"}:
            validated = self._validate_python(args)
            return [self._resolve_executable("python"), *validated[1:]]
        if executable in self._DISALLOWED_DIRECT_TOOLS:
            raise PolicyError(f"请通过 python -m 运行 {executable}，禁止直接调用")
        if executable in {"git", "git.exe"}:
            validated = self._validate_git(args)
            return [self._resolve_executable("git"), *validated[1:]]
        raise PolicyError(f"可执行程序不在允许列表中：{raw_executable}")

    def audit_metadata(self, command: str) -> dict[str, object]:
        """将已允许命令转换为不含自由参数文本的审计元数据。"""

        try:
            args = self.validate(command)
        except PolicyError:
            return {
                "command_valid": False,
                "command_chars": len(command),
            }
        executable = Path(args[0]).name.lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        metadata: dict[str, object] = {
            "command_valid": True,
            "executable": executable,
            "argument_count": len(args) - 1,
        }
        if executable in {"python", "py"}:
            if len(args) >= 3 and args[1] == "-m":
                metadata["execution_kind"] = "module"
                metadata["python_module"] = args[2].lower()
            else:
                metadata["execution_kind"] = "script"
                metadata["script"] = self._safe_relative_script(args[1])
        elif executable == "git":
            metadata["git_subcommand"] = args[1].lower()
        return metadata

    def _safe_relative_script(self, script: str) -> str:
        """把脚本路径规范化为工作区内相对路径；不可解析时返回受限提示。"""
        if self._workspace_policy is not None:
            try:
                resolved = self._workspace_policy.resolve_path(script, must_exist=True)
                return resolved.relative_to(self._workspace_policy.workspace).as_posix()
            except PolicyError:
                pass
        return "<工作区内脚本>"

    def _resolve_executable(self, name: str) -> str:
        """把纯名称解析为可信绝对路径；失败即安全拒绝。"""
        try:
            if name == "python":
                return trusted_python_executable()
            return trusted_path_executable(name, self._subprocess_env)
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc

    def subprocess_environment(self) -> dict[str, str]:
        """Return the same filtered environment used for executable resolution."""

        return dict(self._subprocess_env)

    @staticmethod
    def _is_qualified_path(raw: str) -> bool:
        """可执行程序参数不能包含路径成分或相对片段。"""
        if raw.startswith(("/", "\\")):
            return True
        if CommandPolicy._ABSOLUTE_PATH_PREFIX.match(raw):
            return True
        if "/" in raw or "\\" in raw:
            return True
        return raw in {".", ".."}

    def _validate_python(self, args: list[str]) -> list[str]:
        if len(args) < 3 or args[1] != "-m":
            # 脚本执行：python <工作区内相对 .py 脚本> [参数...]
            return self._validate_python_script(args)
        module = args[2].lower()
        if module not in self._PYTHON_MODULES:
            raise PolicyError(f"Python 模块不在允许列表中：{args[2]}")
        if module == "unittest":
            self._validate_unittest_args(args[3:])
        elif module == "compileall":
            self._validate_tool_params(args[3:], self._COMPILEALL_ALLOWED_OPTIONS, "compileall")
        elif module == "pytest":
            self._validate_tool_params(args[3:], self._PYTEST_ALLOWED_OPTIONS, "pytest")
        elif module == "ruff":
            self._validate_ruff_args(args[3:])
        elif module == "mypy":
            self._validate_tool_params(args[3:], self._MYPY_ALLOWED_OPTIONS, "mypy")
        return args

    def _validate_python_script(self, args: list[str]) -> list[str]:
        """允许运行工作区内相对路径的 .py 脚本。

        脚本必须是工作区内存在的普通 .py 文件（真实解析，含符号链接与
        敏感段检查）；该能力在审批层由权限级别控制（fullaccess 自动放行）。
        """
        if len(args) < 2 or args[1].startswith("-"):
            raise PolicyError("Python 脚本必须以工作区内相对路径开始")
        if not args[1].endswith(".py"):
            raise PolicyError("Python 仅允许运行 .py 脚本")
        if self._workspace_policy is not None:
            try:
                resolved = self._workspace_policy.resolve_path(args[1], must_exist=True)
            except PolicyError:
                raise PolicyError(f"Python 脚本不在工作区内：{args[1]}")
            if not resolved.is_file():
                raise PolicyError(f"Python 脚本不是普通文件：{args[1]}")
            self._require_relative_paths(args[1:], "python")
        else:
            segments = tuple(re.split(r"[\\/]+", args[1]))
            if any(segment.lower() in {"", ".", ".."} for segment in segments):
                raise PolicyError(f"Python 脚本路径包含敏感段：{args[1]}")
            if is_sensitive_workspace_path(args[1]):
                raise PolicyError(f"Python 脚本路径包含敏感段：{args[1]}")
            self._require_relative_paths(args[1:], "python")
        return args

    def _validate_ruff_args(self, params: list[str]) -> None:
        if not params or params[0].lower() != "check":
            raise PolicyError("ruff 仅允许 check 子命令")
        self._validate_tool_params(params[1:], self._RUFF_ALLOWED_OPTIONS, "ruff")

    def _validate_unittest_args(self, params: list[str]) -> None:
        """只允许 discover 或工作区内测试文件，禁止通过 dotted name 导入模块。"""
        discover = bool(params and params[0].lower() == "discover")
        index = 1 if discover else 0
        while index < len(params):
            token = params[index]
            if token.startswith("-"):
                name, separator, value = token.partition("=")
                if name not in self._UNITTEST_ALLOWED_OPTIONS:
                    raise PolicyError(f"unittest 参数不在允许列表：{name}")
                if name in self._UNITTEST_VALUE_OPTIONS:
                    if not separator:
                        index += 1
                        if index >= len(params) or params[index].startswith("-"):
                            raise PolicyError(f"unittest 参数缺少值：{name}")
                        value = params[index]
                    if not value:
                        raise PolicyError(f"unittest 参数缺少值：{name}")
                    if name in self._UNITTEST_PATH_OPTIONS:
                        self._validate_unittest_discovery_path(value)
                elif separator:
                    raise PolicyError(f"unittest 参数不接受值：{name}")
            else:
                if discover:
                    raise PolicyError(f"unittest discover 不接受位置参数：{token}")
                self._validate_unittest_file_target(token)
            index += 1

    def _validate_unittest_discovery_path(self, raw: str) -> None:
        """discover 的起始目录和顶层目录必须真实位于工作区内。"""
        if self._workspace_policy is None:
            self._require_relative_paths([raw], "unittest")
            return
        try:
            resolved = self._workspace_policy.resolve_path(raw, must_exist=True)
        except PolicyError:
            raise PolicyError(f"unittest 路径不在工作区内：{raw}") from None
        if not resolved.is_dir():
            raise PolicyError(f"unittest discover 路径不是目录：{raw}")

    def _validate_unittest_file_target(self, raw: str) -> None:
        """点名运行只接受相对 `.py` 文件，不接受可触发导入的模块名称。"""
        if not raw.lower().endswith(".py"):
            raise PolicyError(f"unittest 点名目标必须是工作区内 .py 文件：{raw}")
        self._require_relative_paths([raw], "unittest")
        if self._workspace_policy is None:
            return
        try:
            resolved = self._workspace_policy.resolve_path(raw, must_exist=True)
        except PolicyError:
            raise PolicyError(f"unittest 测试文件不在工作区内：{raw}") from None
        if not resolved.is_file():
            raise PolicyError(f"unittest 测试目标不是普通文件：{raw}")

    @classmethod
    def is_relaxed_git_metadata_command(cls, args: list[str]) -> bool:
        """判断命令是否只返回工作区 Git 元数据，可在 relaxed 下自动执行。"""
        if not args or Path(args[0]).name.lower().removesuffix(".exe") != "git":
            return False
        index = 1
        while index < len(args) and args[index] in cls._GIT_ALLOWED_PREFIX:
            index += 1
        if index >= len(args):
            return False
        subcommand = args[index].lower()
        tail = args[index + 1 :]
        if subcommand == "status":
            safe_flags = {
                "--short", "--porcelain", "--branch", "--untracked-files",
                "--ignored", "--no-renames",
            }
            safe_values = {
                "--porcelain": {"v1", "v2"},
                "--untracked-files": {"no", "normal", "all"},
                "--ignored": {"no", "traditional", "matching"},
            }
            for token in tail:
                name, separator, value = token.partition("=")
                if name not in safe_flags:
                    return False
                if separator and value not in safe_values.get(name, set()):
                    return False
            return True
        if subcommand == "diff":
            if not any(token in {"--stat", "--name-only"} for token in tail):
                return False
            return all(token in {"--stat", "--name-only", "--no-color"} for token in tail)
        return False

    def _validate_tool_params(
        self,
        params: list[str],
        allowed: set[str],
        label: str,
    ) -> None:
        """选项必须在允许集合内；`--opt=value` 的值不能是外部路径。"""
        for token in params:
            if not token.startswith("-"):
                continue
            name, separator, value = token.partition("=")
            if name not in allowed:
                raise PolicyError(f"{label} 参数不在允许列表：{name}")
            if separator:
                self._reject_path_like_value(value, label)
        self._require_relative_paths(params, label)

    def _validate_git(self, args: list[str]) -> list[str]:
        index = 1
        while index < len(args) and args[index].startswith("-"):
            name = args[index].split("=", 1)[0]
            if name not in self._GIT_ALLOWED_PREFIX:
                raise PolicyError(f"git 全局选项被禁止：{name}")
            index += 1
        if index >= len(args) or args[index].lower() not in self._GIT_READ_ONLY:
            raise PolicyError("Git 仅允许 status、diff、show 和 log")
        subcommand = args[index].lower()
        allowed = self._GIT_SUBCOMMAND_OPTIONS[subcommand]
        for token in args[index + 1 :]:
            if token.startswith("-"):
                name, separator, value = token.partition("=")
                if name in self._GIT_FORBIDDEN:
                    raise PolicyError(f"git 参数被禁止：{name}")
                # git log 的 -N 是 --max-count 简写（-5 等价 -n 5）。
                if subcommand == "log" and re.fullmatch(r"-\d+", token):
                    continue
                if name not in allowed:
                    raise PolicyError(f"git {subcommand} 参数不在允许列表：{name}")
                if separator:
                    self._reject_path_like_value(value, "git")
        return args

    def _require_relative_paths(self, params: list[str], label: str) -> None:
        """路径位置参数必须是在工作区内的相对路径，防止越界读取。"""
        for token in params:
            if token.startswith("-"):
                continue
            if self._workspace_policy is not None:
                try:
                    self._workspace_policy.resolve_path(token, must_exist=False)
                except PolicyError:
                    raise PolicyError(f"{label} 路径不在工作区内：{token}")
                continue
            if (
                self._ABSOLUTE_PATH_PREFIX.match(token)
                or token.startswith(("/", "\\"))
            ):
                raise PolicyError(f"{label} 不接受绝对路径参数：{token}")
            if ".." in token.split("/") or ".." in token.split("\\"):
                raise PolicyError(f"{label} 不接受越界路径参数：{token}")

    def _reject_path_like_value(self, value: str, label: str) -> None:
        """`--opt=value` 的 value 不允许是外部路径或越界片段。"""
        if (
            self._ABSOLUTE_PATH_PREFIX.match(value)
            or value.startswith(("/", "\\"))
            or ".." in value.split("/")
            or ".." in value.split("\\")
        ):
            raise PolicyError(f"{label} 参数值不能是外部路径：{value}")
