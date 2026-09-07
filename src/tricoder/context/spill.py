"""把大型工具结果安全地保存在 TriCoder 管理的会话运行目录。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


_REFERENCE_PATTERN = re.compile(r"spill_[0-9a-f]{32}\Z")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class SpillError(OSError):
    """spill 持久化或回读无法满足安全边界。"""


@dataclass(frozen=True, slots=True)
class SpillRecord:
    """可公开审计的 spill 元数据，不包含正文或本机路径。"""

    reference: str
    byte_count: int
    sha256: str


class ToolResultSpillStore:
    """会话绑定的大结果存储；引用不能跨会话解析。"""

    def __init__(
        self,
        root: Path,
        session_id: str,
        *,
        max_entry_bytes: int = 2_000_000,
        max_session_bytes: int = 10_000_000,
    ) -> None:
        raw_root = Path(root)
        if not raw_root.is_absolute():
            raise SpillError("spill 运行目录必须是绝对路径")
        if not isinstance(session_id, str) or not session_id:
            raise SpillError("spill 会话标识无效")
        for name, value in (
            ("max_entry_bytes", max_entry_bytes),
            ("max_session_bytes", max_session_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SpillError(f"{name} 必须是正整数")
        if max_entry_bytes > max_session_bytes:
            raise SpillError("spill 单项上限不能大于会话上限")

        self.root = Path(os.path.abspath(raw_root))
        session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        self.session_dir = self.root / f"session_{session_key}"
        self.max_entry_bytes = max_entry_bytes
        self.max_session_bytes = max_session_bytes
        self._call_references: dict[str, str] = {}
        try:
            self._reject_link_components(self.root)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._reject_link_components(self.root)
            self.session_dir.mkdir(exist_ok=True, mode=0o700)
            self._reject_link_components(self.session_dir)
        except SpillError:
            raise
        except OSError as exc:
            raise SpillError("无法准备 spill 运行目录") from exc

    def persist(self, call_id: str, content: str) -> SpillRecord:
        """原子保存一份正文，并返回不含路径的受控引用。"""

        if not isinstance(call_id, str) or not call_id:
            raise SpillError("spill 调用标识无效")
        if not isinstance(content, str):
            raise SpillError("spill 正文必须是文本")
        if call_id in self._call_references:
            raise SpillError("重复的 spill 调用标识")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_entry_bytes:
            raise SpillError("spill 单项大小超过上限")
        if self._stored_bytes() + len(encoded) > self.max_session_bytes:
            raise SpillError("spill 会话容量超过上限")

        reference = self._new_reference()
        final_path = self._path_for_reference(reference)
        temporary = self.session_dir / f".{reference}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            self._reject_link_components(self.session_dir)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                descriptor = None
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            # 用“不覆盖”硬链接原子发布；即使随机引用发生 TOCTOU 碰撞，
            # 也只会失败，绝不会覆盖已存在的 spill 正文。
            os.link(temporary, final_path, follow_symlinks=False)
            temporary.unlink()
            try:
                os.chmod(final_path, 0o600)
            except OSError:
                # Windows 的权限位不是安全边界；目录仍受当前用户状态目录约束。
                if os.name != "nt":
                    raise
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SpillError("无法安全写入 spill 结果") from exc

        self._call_references[call_id] = reference
        return SpillRecord(
            reference,
            len(encoded),
            hashlib.sha256(encoded).hexdigest(),
        )

    def preview(
        self,
        reference: str,
        *,
        offset: int = 0,
        max_chars: int = 2_000,
    ) -> str:
        """从当前会话引用读取一个有界字符片段。"""

        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise SpillError("spill offset 必须是非负整数")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise SpillError("spill max_chars 必须是正整数")
        path = self._path_for_reference(reference)
        try:
            self._reject_link_components(path)
            metadata = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise SpillError("spill 引用不是普通文件")
            content = path.read_text(encoding="utf-8")
        except SpillError:
            raise
        except (OSError, UnicodeError) as exc:
            raise SpillError("spill 引用不可读取") from exc
        if offset > len(content):
            raise SpillError("spill offset 超出正文范围")
        return content[offset : offset + max_chars]

    def cleanup(self) -> None:
        """仅清理当前会话目录中的系统生成文件。"""

        try:
            self._reject_link_components(self.session_dir)
            if not self.session_dir.exists():
                self._call_references.clear()
                self.session_dir.mkdir(exist_ok=True, mode=0o700)
                self._reject_link_components(self.session_dir)
                return
            for child in self.session_dir.iterdir():
                if child.is_file() and (
                    _REFERENCE_PATTERN.fullmatch(child.stem)
                    or (child.name.startswith(".spill_") and child.name.endswith(".tmp"))
                ):
                    self._reject_link_components(child)
                    child.unlink()
            try:
                self.session_dir.rmdir()
            except OSError:
                # 未识别文件存在时拒绝递归删除，仅保留目录。
                pass
            self._call_references.clear()
            self.session_dir.mkdir(exist_ok=True, mode=0o700)
            self._reject_link_components(self.session_dir)
        except SpillError:
            raise
        except OSError as exc:
            raise SpillError("无法安全清理 spill 结果") from exc

    def _stored_bytes(self) -> int:
        total = 0
        try:
            for child in self.session_dir.glob("spill_*.txt"):
                self._reject_link_components(child)
                metadata = child.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SpillError("spill 目录包含非法条目")
                total += metadata.st_size
        except SpillError:
            raise
        except OSError as exc:
            raise SpillError("无法核验 spill 会话容量") from exc
        return total

    def _new_reference(self) -> str:
        for _attempt in range(128):
            reference = f"spill_{secrets.token_hex(16)}"
            if not self._path_for_reference(reference).exists():
                return reference
        raise SpillError("无法分配 spill 引用")

    def _path_for_reference(self, reference: str) -> Path:
        if not isinstance(reference, str) or not _REFERENCE_PATTERN.fullmatch(reference):
            raise SpillError("spill 引用无效或不属于当前会话")
        return self.session_dir / f"{reference}.txt"

    @staticmethod
    def _reject_link_components(path: Path) -> None:
        """拒绝路径链中的符号链接与 Windows reparse point。"""

        absolute = Path(os.path.abspath(path))
        chain = [absolute]
        chain.extend(absolute.parents)
        for component in reversed(chain):
            if not os.path.lexists(component):
                continue
            metadata = os.lstat(component)
            attributes = getattr(metadata, "st_file_attributes", 0)
            if stat.S_ISLNK(metadata.st_mode) or (
                isinstance(attributes, int)
                and attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise SpillError("spill 运行目录不能包含链接或 junction")
