"""Coding Agent 可调用的本地工具。"""

from __future__ import annotations

import copy
import difflib
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tricoder.models import ToolDefinition, ToolResult
from tricoder.policy import CommandPolicy, PolicyError, WorkspacePolicy


Approver = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class _ToolRegistration:
    """将公开工具定义与唯一对应的本地处理器绑定。"""

    definition: ToolDefinition
    handler: Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    """跨平台比较文件系统对象身份所需的稳定字段。"""

    device: int
    inode: int


def _stat_identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(metadata.st_dev, metadata.st_ino)


def _is_windows() -> bool:
    """隔离平台分支，便于测试无法安全降级的路径。"""

    return os.name == "nt"


class _DirectoryBinding:
    """把审批后的发布操作绑定到审批前确认的父目录对象。"""

    def __init__(self, parent: Path) -> None:
        self.parent = parent

    @classmethod
    def open(cls, workspace: Path, parent: Path) -> "_DirectoryBinding":
        if _is_windows():
            return _WindowsDirectoryBinding(workspace, parent)
        if not {
            os.chmod,
            os.rename,
            os.open,
            os.link,
            os.unlink,
            os.stat,
        }.issubset(os.supports_dir_fd):
            raise PolicyError("当前平台无法建立安全目录绑定，拒绝写入")
        return _PosixDirectoryBinding(parent)

    def close(self) -> None:
        raise NotImplementedError

    def verify_parent(self, parent: Path) -> bool:
        raise NotImplementedError

    def read_text(self, name: str) -> tuple[str, _FileIdentity, int]:
        raise NotImplementedError

    def target_exists(self, name: str) -> bool:
        raise NotImplementedError

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        raise NotImplementedError

    def replace(self, temporary_name: str, target_name: str) -> None:
        raise NotImplementedError

    def link(self, temporary_name: str, target_name: str) -> None:
        raise NotImplementedError

    def unlink(self, temporary_name: str) -> None:
        raise NotImplementedError

    @staticmethod
    def _temporary_names(target_name: str) -> Any:
        for _attempt in range(128):
            yield f".{target_name}.{secrets.token_hex(8)}.tmp"
        raise OSError("无法分配安全的临时文件名")


class _PosixDirectoryBinding(_DirectoryBinding):
    """POSIX 目录句柄绑定；所有发布路径都相对于同一目录句柄。"""

    def __init__(self, parent: Path) -> None:
        super().__init__(parent)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._fd = os.open(parent, flags)
        except OSError as exc:
            raise PolicyError("无法建立安全目录绑定，拒绝写入") from exc
        metadata = os.fstat(self._fd)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(self._fd)
            raise PolicyError("写入目标的父路径不是目录")
        self._identity = _stat_identity(metadata)

    def close(self) -> None:
        os.close(self._fd)

    def verify_parent(self, parent: Path) -> bool:
        if parent != self.parent:
            return False
        try:
            return _stat_identity(parent.stat()) == self._identity
        except OSError:
            return False

    def read_text(self, name: str) -> tuple[str, _FileIdentity, int]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=self._fd)
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            metadata = os.fstat(file.fileno())
            content = file.read()
        return content, _stat_identity(metadata), metadata.st_mode

    def target_exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        for temporary_name in self._temporary_names(target_name):
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=self._fd,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as temporary:
                    temporary.write(content)
                os.chmod(
                    temporary_name,
                    stat.S_IMODE(mode),
                    dir_fd=self._fd,
                )
            except BaseException:
                try:
                    os.unlink(temporary_name, dir_fd=self._fd)
                except OSError:
                    pass
                raise
            return temporary_name
        raise OSError("无法分配安全的临时文件名")

    def replace(self, temporary_name: str, target_name: str) -> None:
        os.rename(
            temporary_name,
            target_name,
            src_dir_fd=self._fd,
            dst_dir_fd=self._fd,
        )

    def link(self, temporary_name: str, target_name: str) -> None:
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=self._fd,
            dst_dir_fd=self._fd,
            follow_symlinks=False,
        )

    def unlink(self, temporary_name: str) -> None:
        os.unlink(temporary_name, dir_fd=self._fd)


class _WindowsDirectoryBinding(_DirectoryBinding):
    """用不共享删除权限的目录句柄锁住 Windows 路径的每个目录组件。"""

    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    def __init__(self, workspace: Path, parent: Path) -> None:
        super().__init__(parent)
        if not parent.is_relative_to(workspace):
            raise PolicyError("写入目标的父目录不属于工作区")
        self._handles: list[int] = []
        try:
            for component in self._path_chain(parent):
                handle, identity = self._open_handle(component)
                self._handles.append(handle)
                if component == parent:
                    self._identity = identity
        except (OSError, AttributeError) as exc:
            self.close()
            raise PolicyError("Windows 无法建立防重命名目录绑定，拒绝写入") from exc
        if not hasattr(self, "_identity"):
            self.close()
            raise PolicyError("Windows 无法确认父目录身份，拒绝写入")

    @staticmethod
    def _path_chain(path: Path) -> list[Path]:
        parts = path.parts
        if not parts:
            return []
        current = Path(parts[0])
        chain = [current]
        for part in parts[1:]:
            current = current / part
            chain.append(current)
        return chain

    @classmethod
    def _open_handle(cls, path: Path) -> tuple[int, _FileIdentity]:
        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            cls._FILE_READ_ATTRIBUTES,
            cls._FILE_SHARE_READ | cls._FILE_SHARE_WRITE,
            None,
            cls._OPEN_EXISTING,
            cls._FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _ByHandleFileInformation()
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        get_information.restype = wintypes.BOOL
        if not get_information(handle, ctypes.byref(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        identity = _FileIdentity(
            information.volume_serial_number,
            (information.file_index_high << 32) | information.file_index_low,
        )
        return int(handle), identity

    @staticmethod
    def _close_handle(handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)

    def close(self) -> None:
        for handle in reversed(getattr(self, "_handles", [])):
            self._close_handle(handle)
        self._handles = []

    def verify_parent(self, parent: Path) -> bool:
        if parent != self.parent:
            return False
        try:
            handle, identity = self._open_handle(parent)
        except OSError:
            return False
        try:
            return identity == self._identity
        finally:
            self._close_handle(handle)

    def read_text(self, name: str) -> tuple[str, _FileIdentity, int]:
        with (self.parent / name).open("r", encoding="utf-8") as file:
            metadata = os.fstat(file.fileno())
            content = file.read()
        return content, _stat_identity(metadata), metadata.st_mode

    def target_exists(self, name: str) -> bool:
        return os.path.lexists(self.parent / name)

    def create_temporary(self, target_name: str, content: str, mode: int) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for temporary_name in self._temporary_names(target_name):
            path = self.parent / temporary_name
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as temporary:
                    temporary.write(content)
                os.chmod(path, stat.S_IMODE(mode))
            except BaseException:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            return temporary_name
        raise OSError("无法分配安全的临时文件名")

    def replace(self, temporary_name: str, target_name: str) -> None:
        os.replace(self.parent / temporary_name, self.parent / target_name)

    def link(self, temporary_name: str, target_name: str) -> None:
        os.link(self.parent / temporary_name, self.parent / target_name)

    def unlink(self, temporary_name: str) -> None:
        os.unlink(self.parent / temporary_name)


@dataclass(slots=True)
class ToolContext:
    """工具执行所需的策略、审批与资源限制。"""

    workspace_policy: WorkspacePolicy
    command_policy: CommandPolicy
    approver: Approver
    read_only: bool = False
    timeout: float = 30.0
    max_output_chars: int = 20_000


class ToolRegistry:
    """按固定名称分发工具，统一把预期错误转换为 ToolResult。"""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._registrations = (
            _ToolRegistration(
                ToolDefinition(
                    "list_files",
                    "列出工作区内指定目录的条目。",
                    self._schema({"path": {"type": "string"}}),
                ),
                self._list_files,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "read_file",
                    "读取工作区内 UTF-8 文本文件。",
                    self._schema({"path": {"type": "string"}}, ["path"]),
                ),
                self._read_file,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "search_text",
                    "在工作区目录内搜索文本。",
                    self._schema(
                        {
                            "path": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        ["query"],
                    ),
                ),
                self._search_text,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "edit_file",
                    "经审批后精确替换工作区内文件的一段文本。",
                    self._schema(
                        {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        ["path", "old_text", "new_text"],
                    ),
                ),
                self._edit_file,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "create_file",
                    "经审批后在工作区内创建新的 UTF-8 文件。",
                    self._schema(
                        {"path": {"type": "string"}, "content": {"type": "string"}},
                        ["path", "content"],
                    ),
                ),
                self._create_file,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "run_command",
                    "经审批后在工作区内运行受策略允许的命令。",
                    self._schema(
                        {"command": {"type": "string"}, "cwd": {"type": "string"}},
                        ["command"],
                    ),
                ),
                self._run_command,
            ),
            _ToolRegistration(
                ToolDefinition(
                    "finish",
                    "提交本轮任务的文字总结。",
                    self._schema({"summary": {"type": "string"}}, ["summary"]),
                ),
                self._finish,
            ),
        )
    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回顺序固定且与内部注册表隔离的公开工具定义。"""
        return tuple(
            self._public_definition(registration.definition)
            for registration in self._registrations
        )

    def contains(self, name: str) -> bool:
        """判断名称是否在公开注册表中。"""
        return any(
            registration.definition.name == name
            for registration in self._registrations
        )

    def describe(self, name: str) -> ToolDefinition | None:
        """只返回注册表中静态声明的公开定义。"""
        for registration in self._registrations:
            if registration.definition.name == name:
                return self._public_definition(registration.definition)
        return None

    @staticmethod
    def _public_definition(definition: ToolDefinition) -> ToolDefinition:
        """复制嵌套 Schema，避免公开调用方修改注册表的校验依据。"""
        return ToolDefinition(
            definition.name,
            definition.description,
            copy.deepcopy(definition.parameters),
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        registration = next(
            (
                item
                for item in self._registrations
                if item.definition.name == name
            ),
            None,
        )
        if registration is None:
            return ToolResult(False, f"未知工具：{name}")
        try:
            self._validate_arguments(registration.definition.parameters, arguments)
            return registration.handler(arguments)
        except (PolicyError, OSError, UnicodeError, ValueError, TypeError) as exc:
            return ToolResult(False, str(exc))

    @staticmethod
    def _schema(
        properties: dict[str, dict[str, str]],
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        """构建当前简单工具所需的统一对象 Schema。"""
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: object) -> None:
        """在进入处理器前校验当前工具 Schema 支持的基础类型。"""
        if schema.get("type") != "object" or not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")

        properties = schema["properties"]
        for name in schema["required"]:
            if name not in arguments:
                raise ValueError(f"缺少必填参数：{name}")
        if not schema["additionalProperties"]:
            extras = set(arguments) - set(properties)
            if extras:
                raise ValueError(f"不支持额外参数：{sorted(extras)[0]}")

        validators: dict[str, type[object]] = {
            "string": str,
            "integer": int,
            "boolean": bool,
        }
        for name, value in arguments.items():
            expected = properties[name]["type"]
            expected_type = validators.get(expected)
            if expected_type is None:
                raise ValueError(f"不支持的参数类型：{expected}")
            if not isinstance(value, expected_type) or (
                expected == "integer" and isinstance(value, bool)
            ):
                raise ValueError(f"参数 {name} 必须是 {expected}")

    def _list_files(self, arguments: dict[str, Any]) -> ToolResult:
        directory = self.context.workspace_policy.resolve_path(
            str(arguments.get("path", ".")),
        )
        if not directory.is_dir():
            return ToolResult(False, "list_files 的目标必须是目录")
        entries: list[str] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            try:
                self.context.workspace_policy.resolve_path(child)
            except PolicyError:
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.relative_to(self.context.workspace_policy.workspace)}{suffix}")
        return ToolResult(True, self._bounded("\n".join(entries) or "(空目录)"))

    def _read_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.context.workspace_policy.resolve_path(self._required_str(arguments, "path"))
        if not path.is_file():
            return ToolResult(False, "read_file 的目标必须是文件")
        content = path.read_text(encoding="utf-8")
        return ToolResult(True, self._bounded(content))

    def _search_text(self, arguments: dict[str, Any]) -> ToolResult:
        query = self._required_str(arguments, "query")
        root = self.context.workspace_policy.resolve_path(str(arguments.get("path", ".")))
        if not root.is_dir():
            return ToolResult(False, "search_text 的 path 必须是目录")

        matches: list[str] = []
        for current_root, directories, files in os.walk(root, followlinks=False):
            current = Path(current_root)
            # 在进入子目录前过滤敏感目录和可能逃逸的链接。
            allowed_directories: list[str] = []
            for name in directories:
                try:
                    candidate = self.context.workspace_policy.resolve_path(current / name)
                except PolicyError:
                    continue
                if not candidate.is_symlink():
                    allowed_directories.append(name)
            directories[:] = allowed_directories

            for name in files:
                try:
                    path = self.context.workspace_policy.resolve_path(current / name)
                    text = path.read_text(encoding="utf-8")
                except (PolicyError, UnicodeError, OSError):
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        relative = path.relative_to(self.context.workspace_policy.workspace)
                        matches.append(f"{relative}:{line_number}: {line.strip()}")
                        if len(matches) >= 200:
                            return ToolResult(True, self._bounded("\n".join(matches)))
        return ToolResult(True, self._bounded("\n".join(matches) or "(无匹配)"))

    def _edit_file(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止编辑文件")
        raw_path = self._required_str(arguments, "path")
        path = self.context.workspace_policy.resolve_path(raw_path)
        if not path.is_file():
            return ToolResult(False, "edit_file 的目标必须是文件")
        old_text = self._required_str(arguments, "old_text")
        new_text = self._required_str(arguments, "new_text", allow_empty=True)
        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            path.parent,
        )
        temporary_name: str | None = None
        committed = False
        close_warning = False
        try:
            preapproved = self.context.workspace_policy.resolve_path(raw_path)
            if preapproved != path or not binding.verify_parent(preapproved.parent):
                return ToolResult(False, "目标父目录身份发生变化，拒绝请求审批")
            original, original_identity, original_mode = binding.read_text(path.name)
            if original.count(old_text) != 1:
                return ToolResult(
                    False,
                    "old_text 必须在目标文件中恰好出现一次，请重新读取文件",
                )
            updated = original.replace(old_text, new_text, 1)
            relative = path.relative_to(self.context.workspace_policy.workspace)
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=str(relative),
                    tofile=str(relative),
                )
            )
            if not self.context.approver("edit_file", diff):
                return ToolResult(False, "用户拒绝了文件修改")

            verified = self.context.workspace_policy.resolve_path(raw_path)
            if not binding.verify_parent(verified.parent) or verified != path:
                return ToolResult(False, "审批后目标父目录身份发生变化，拒绝写入")
            current, current_identity, _current_mode = binding.read_text(path.name)
            if current_identity != original_identity or current != original:
                return ToolResult(False, "审批后目标文件发生变化，请重新读取并审批")

            temporary_name = binding.create_temporary(
                path.name,
                updated,
                original_mode,
            )
            binding.replace(temporary_name, path.name)
            committed = True
            temporary_name = None
        finally:
            if temporary_name is not None:
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            try:
                binding.close()
            except OSError:
                if not committed:
                    raise
                close_warning = True
        output = f"已修改 {relative}"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，文件修改已提交，请在验证时检查目录"
        return ToolResult(True, output, relative.as_posix())

    def _create_file(self, arguments: dict[str, Any]) -> ToolResult:
        """经审批后，以不可覆盖的原子发布方式创建 UTF-8 文件。"""
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止创建文件")
        raw_path = self._required_str(arguments, "path")
        path = self.context.workspace_policy.resolve_path(
            raw_path,
            must_exist=False,
        )
        content = self._required_str(arguments, "content", allow_empty=True)
        if not path.parent.is_dir():
            return ToolResult(False, "create_file 的父目录必须存在")
        if path.exists():
            return ToolResult(False, "create_file 的目标文件已存在，拒绝覆盖")

        relative = path.relative_to(self.context.workspace_policy.workspace)
        diff = (
            "".join(
                difflib.unified_diff(
                    [],
                    content.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=str(relative),
                )
            )
            if content
            else (
                f"--- /dev/null\n+++ {relative}\n"
                "@@ -0,0 +0,0 @@\n"
                "（创建空文件，内容为 0 字符）\n"
            )
        )

        binding = _DirectoryBinding.open(
            self.context.workspace_policy.workspace,
            path.parent,
        )
        temporary_name: str | None = None
        committed = False
        cleanup_warning = False
        close_warning = False
        try:
            preapproved = self.context.workspace_policy.resolve_path(
                raw_path,
                must_exist=False,
            )
            if preapproved != path or not binding.verify_parent(preapproved.parent):
                return ToolResult(False, "目标父目录身份发生变化，拒绝请求审批")
            if not self.context.approver("create_file", diff):
                return ToolResult(False, "用户拒绝了创建文件")

            verified = self.context.workspace_policy.resolve_path(
                raw_path,
                must_exist=False,
            )
            if not binding.verify_parent(verified.parent) or verified != path:
                return ToolResult(False, "审批后目标父目录身份发生变化，拒绝写入")
            if binding.target_exists(path.name):
                return ToolResult(False, "审批后目标文件已存在，拒绝覆盖")

            temporary_name = binding.create_temporary(path.name, content, 0o600)
            try:
                binding.link(temporary_name, path.name)
            except OSError:
                return ToolResult(False, "create_file 无法原子发布文件，拒绝覆盖")
            committed = True
            try:
                binding.unlink(temporary_name)
            except OSError:
                cleanup_warning = True
            temporary_name = None
        finally:
            if temporary_name is not None and not committed:
                try:
                    binding.unlink(temporary_name)
                except OSError:
                    pass
            try:
                binding.close()
            except OSError:
                if not committed:
                    raise
                close_warning = True
        output = f"已创建 {relative}"
        if cleanup_warning:
            output += "；清理警告：临时链接未能删除，请在验证时检查目录"
        if close_warning:
            output += "；关闭警告：目录绑定未能正常关闭，文件创建已提交，请在验证时检查目录"
        return ToolResult(True, output, relative.as_posix())

    def _run_command(self, arguments: dict[str, Any]) -> ToolResult:
        if self.context.read_only:
            return ToolResult(False, "只读模式禁止执行命令")
        command = self._required_str(arguments, "command")
        args = self.context.command_policy.validate(command)
        cwd = self.context.workspace_policy.resolve_path(str(arguments.get("cwd", ".")))
        if not cwd.is_dir():
            return ToolResult(False, "命令工作目录必须是目录")
        detail = f"目录：{cwd}\n命令：{command}\n超时：{self.context.timeout:g} 秒"
        if not self.context.approver("run_command", detail):
            return ToolResult(False, "用户拒绝了命令执行")
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.context.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"命令执行超过 {self.context.timeout:g} 秒")
        output = (
            f"退出码：{completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        return ToolResult(completed.returncode == 0, self._bounded(output))

    def _finish(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(True, self._required_str(arguments, "summary"))

    def _bounded(self, text: str) -> str:
        if len(text) <= self.context.max_output_chars:
            return text
        omitted = len(text) - self.context.max_output_chars
        return f"{text[: self.context.max_output_chars]}\n...（已截断 {omitted} 个字符）"

    @staticmethod
    def _required_str(
        arguments: dict[str, Any],
        name: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ValueError(f"{name} 必须是字符串且不能为空")
        return value
