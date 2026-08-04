"""工作区路径与命令执行安全策略。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path


class PolicyError(PermissionError):
    """表示动作超出了 MVP 允许的安全边界。"""


class WorkspacePolicy:
    """确保模型只能访问明确指定的工作区。"""

    _SENSITIVE_PARTS = {
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
    }
    _SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

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
            if lowered == ".env.example":
                continue
            if lowered in self._SENSITIVE_PARTS or Path(lowered).suffix in self._SENSITIVE_SUFFIXES:
                raise PolicyError(f"拒绝访问敏感路径：{part}")


class CommandPolicy:
    """只允许不改变依赖或 Git 状态的测试与检查命令。

    白名单按工具维护明确允许的参数集合；可执行程序必须是 PATH 中的纯名称，
    校验通过后解析为可信绝对路径交给 subprocess，审批与审计均展示实际程序。
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
    _GIT_FORBIDDEN_FLAGS = {"--no-index", "--ext-diff", "--textconv", "--paginate"}
    # 每工具独立的允许参数集合；不在集合内的选项一律拒绝，
    # 避免通用黑名单随工具版本演化而漏禁。
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
            metadata["python_module"] = args[2].lower()
        elif executable == "git":
            metadata["git_subcommand"] = args[1].lower()
        return metadata

    def _resolve_executable(self, name: str) -> str:
        """把纯名称解析为 PATH 中的可信绝对路径；失败即安全拒绝。"""
        resolved = shutil.which(name)
        if resolved is None:
            raise PolicyError(f"找不到可信的 {name} 可执行程序")
        return str(Path(resolved).resolve())

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
            raise PolicyError("Python 仅允许通过 -m 运行测试或静态检查模块")
        module = args[2].lower()
        if module not in self._PYTHON_MODULES:
            raise PolicyError(f"Python 模块不在允许列表中：{args[2]}")
        if module == "pytest":
            self._validate_tool_params(args[3:], self._PYTEST_ALLOWED_OPTIONS, "pytest")
        elif module == "ruff":
            self._validate_ruff_args(args[3:])
        elif module == "mypy":
            self._validate_tool_params(args[3:], self._MYPY_ALLOWED_OPTIONS, "mypy")
        return args

    def _validate_ruff_args(self, params: list[str]) -> None:
        if not params or params[0].lower() != "check":
            raise PolicyError("ruff 仅允许 check 子命令")
        self._validate_tool_params(params[1:], self._RUFF_ALLOWED_OPTIONS, "ruff")

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
        for token in args[index:]:
            name = token.split("=", 1)[0]
            if name in self._GIT_FORBIDDEN_FLAGS:
                raise PolicyError(f"git 参数被禁止：{name}")
            if name.startswith("-C") or (
                name.startswith("-c") and not name.startswith("--")
            ):
                raise PolicyError(f"git 参数被禁止：{name}")
        return args

    def _require_relative_paths(self, params: list[str], label: str) -> None:
        """路径位置参数必须是在工作区内的相对路径，防止越界读取。"""
        for token in params:
            if token.startswith("-"):
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
