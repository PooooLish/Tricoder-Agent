"""工作区路径与命令执行安全策略。"""

from __future__ import annotations

import os
import re
import shlex
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
    """只允许不改变依赖或 Git 状态的测试与检查命令。"""

    _META_PATTERN = re.compile(r"[|&;><`\r\n]")
    _PYTHON_MODULES = {"unittest", "pytest", "compileall", "ruff", "mypy"}
    _DIRECT_TOOLS = {"pytest", "pytest.exe", "ruff", "ruff.exe", "mypy", "mypy.exe"}
    _GIT_READ_ONLY = {"status", "diff", "show", "log"}

    def validate(self, command: str) -> list[str]:
        """返回可交给 `subprocess` 的参数数组，否则抛出策略错误。"""

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

        executable = Path(args[0]).name.lower()
        if executable in {"python", "python.exe", "py", "py.exe"}:
            return self._validate_python(args)
        if executable in self._DIRECT_TOOLS:
            return args
        if executable in {"git", "git.exe"}:
            return self._validate_git(args)
        raise PolicyError(f"可执行程序不在允许列表中：{args[0]}")

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

    def _validate_python(self, args: list[str]) -> list[str]:
        if len(args) < 3 or args[1] != "-m":
            raise PolicyError("Python 仅允许通过 -m 运行测试或静态检查模块")
        if args[2].lower() not in self._PYTHON_MODULES:
            raise PolicyError(f"Python 模块不在允许列表中：{args[2]}")
        return args

    def _validate_git(self, args: list[str]) -> list[str]:
        if len(args) < 2 or args[1].lower() not in self._GIT_READ_ONLY:
            raise PolicyError("Git 仅允许 status、diff、show 和 log")
        return args
