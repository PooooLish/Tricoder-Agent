"""SQLite 会话存储及可持久化内容的安全摘要。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tricoder.models import SessionMemory, SessionRecord


class SessionError(ValueError):
    """会话名称或持久化数据不符合约束时的安全错误。"""


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def default_sessions_db(environment: Mapping[str, str] | None = None) -> Path:
    """返回系统状态目录中的绝对数据库路径，不使用目标工作区。"""
    values = os.environ if environment is None else environment
    if _is_windows():
        state_home = values.get("LOCALAPPDATA")
        user_profile = values.get("USERPROFILE")
        if state_home:
            base = Path(state_home)
        elif user_profile:
            base = Path(user_profile) / "AppData" / "Local"
        else:
            base = Path.home() / "AppData" / "Local"
        return (base / "TriCoder" / "sessions.db").resolve()

    state_home = values.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return (base / "tricoder" / "sessions.db").resolve()


def validate_session_name(value: str) -> str:
    """规范化名称，并拒绝空白、过长及控制字符输入。"""
    if not isinstance(value, str):
        raise SessionError("会话名称必须是文本")
    name = value.strip()
    if not 1 <= len(name) <= 50:
        raise SessionError("会话名称长度必须在 1 到 50 个字符之间")
    if any(not character.isprintable() for character in name):
        raise SessionError("会话名称不能包含控制字符")
    return name


def safe_requirement_summary(text: str, max_chars: int = 500) -> str:
    """以受控长度提示替代全部任务原文，供 SQLite 持久化使用。"""
    if not isinstance(text, str):
        raise SessionError("需求摘要必须是文本")
    if max_chars < 1:
        raise SessionError("摘要长度上限必须为正数")
    return _withheld_summary(text)


def safe_result_summary(text: str, max_chars: int = 2_000) -> str:
    """以受控长度提示替代全部结果原文，供 SQLite 持久化使用。"""
    if not isinstance(text, str):
        raise SessionError("结果摘要必须是文本")
    if max_chars < 1:
        raise SessionError("摘要长度上限必须为正数")
    return _withheld_summary(text)


def _withheld_summary(text: str) -> str:
    """以长度提示替代不应写入 SQLite 的自由文本。"""
    return f"原文未持久化（共 {len(text)} 字符）"


def _path_is_within(path: Path, directory: Path) -> bool:
    """以平台大小写规则判断 path 是否位于 directory 内。"""
    normalized_path = os.path.normcase(str(path.resolve()))
    normalized_directory = os.path.normcase(str(directory.resolve()))
    try:
        return os.path.commonpath((normalized_path, normalized_directory)) == normalized_directory
    except ValueError:
        return False


def _require_text(row: sqlite3.Row, column: str) -> str:
    """拒绝 SQLite BLOB 或 NULL，避免损坏数据伪装为正常文本。"""
    value = row[column]
    if not isinstance(value, str):
        raise SessionError("会话持久化数据损坏")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    """通过短连接和显式事务管理本地 SQLite 会话数据。"""

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        path = Path(database_path)
        if not path.is_absolute():
            raise SessionError("会话数据库路径必须为绝对路径")
        self.database_path = path.resolve()
        self._clock = clock
        self._id_factory = id_factory

    def initialize(self, workspace: Path) -> None:
        """校验目标工作区后，幂等创建表结构并启用外键约束。"""
        workspace_path = Path(workspace).resolve()
        if _path_is_within(self.database_path, workspace_path):
            raise SessionError("会话数据库不能位于目标工作区内")
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        workspace TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS session_memory (
                        session_id TEXT PRIMARY KEY,
                        summary TEXT NOT NULL DEFAULT '',
                        requirements_summary TEXT NOT NULL DEFAULT '',
                        last_task_summary TEXT NOT NULL DEFAULT '',
                        modified_files_json TEXT NOT NULL DEFAULT '[]',
                        verification TEXT NOT NULL DEFAULT '未运行',
                        permission TEXT NOT NULL DEFAULT 'strict',
                        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                    );
                    """
                )
                self._ensure_permission_column(connection)
                connection.commit()
        except (OSError, sqlite3.Error) as error:
            raise SessionError("会话数据库初始化失败") from error

    @staticmethod
    def _ensure_permission_column(connection: sqlite3.Connection) -> None:
        """为旧库补齐 permission 列；已存在则跳过。"""
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(session_memory)")
        }
        if "permission" not in columns:
            connection.execute(
                "ALTER TABLE session_memory "
                "ADD COLUMN permission TEXT NOT NULL DEFAULT 'strict'"
            )

    def create(self, name: str, workspace: Path, provider: str, model: str) -> SessionRecord:
        """创建会话及其独立的空记忆记录。"""
        record = self.prepare_record(name, workspace, provider, model)
        return self.insert_prepared(record)

    def prepare_record(
        self,
        name: str,
        workspace: Path,
        provider: str,
        model: str,
    ) -> SessionRecord:
        """只校验并预分配真实标识，不创建目录、不连接 SQLite。"""
        validated_name = validate_session_name(name)
        workspace_path = Path(workspace).resolve()
        if _path_is_within(self.database_path, workspace_path):
            raise SessionError("会话数据库不能位于目标工作区内")
        session_id = self._id_factory()
        if not isinstance(session_id, str) or not session_id:
            raise SessionError("会话标识无效")
        if not isinstance(provider, str) or not provider.strip():
            raise SessionError("Provider 不能为空")
        if not isinstance(model, str) or not model.strip():
            raise SessionError("模型不能为空")
        created_at = self._clock()
        return SessionRecord(
            session_id,
            validated_name,
            workspace_path,
            provider.strip(),
            model.strip(),
            created_at,
            created_at,
        )

    def insert_prepared(self, record: SessionRecord) -> SessionRecord:
        """事务插入已准备的会话和空记忆；此处才产生 SQLite 写入。"""
        if not isinstance(record, SessionRecord):
            raise SessionError("会话记录无效")
        if not isinstance(record.id, str) or not record.id:
            raise SessionError("会话标识无效")
        if not isinstance(record.created_at, str) or not isinstance(record.updated_at, str):
            raise SessionError("会话时间无效")
        validated_name = validate_session_name(record.name)
        workspace_path = Path(record.workspace).resolve()
        if _path_is_within(self.database_path, workspace_path):
            raise SessionError("会话数据库不能位于目标工作区内")
        if not isinstance(record.provider, str) or not record.provider.strip():
            raise SessionError("Provider 不能为空")
        if not isinstance(record.model, str) or not record.model.strip():
            raise SessionError("模型不能为空")
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (id, name, workspace, provider, model, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        validated_name,
                        str(workspace_path),
                        record.provider.strip(),
                        record.model.strip(),
                        record.created_at,
                        record.updated_at,
                    ),
                )
                connection.execute("INSERT INTO session_memory (session_id) VALUES (?)", (record.id,))
        except sqlite3.Error as error:
            raise SessionError("会话创建失败") from error
        return SessionRecord(
            record.id,
            validated_name,
            workspace_path,
            record.provider.strip(),
            record.model.strip(),
            record.created_at,
            record.updated_at,
        )

    def get(self, session_id: str) -> SessionRecord:
        """按稳定标识读取会话元数据。"""
        row = self._fetchone(
            "SELECT id, name, workspace, provider, model, created_at, updated_at FROM sessions WHERE id = ?",
            (session_id,),
        )
        if row is None:
            raise SessionError("找不到会话")
        return self._record_from_row(row)

    def list_all(self) -> list[SessionRecord]:
        """以最近更新优先的顺序列出全部会话。"""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, name, workspace, provider, model, created_at, updated_at
                    FROM sessions ORDER BY updated_at DESC, id DESC
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise SessionError("会话读取失败") from error
        return [self._record_from_row(row) for row in rows]

    def latest_for_workspace(self, workspace: Path) -> SessionRecord | None:
        """返回指定工作区最近更新的会话，未创建时返回 None。"""
        row = self._fetchone(
            """
            SELECT id, name, workspace, provider, model, created_at, updated_at
            FROM sessions WHERE workspace = ? ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (str(Path(workspace).resolve()),),
        )
        return None if row is None else self._record_from_row(row)

    def rename(self, session_id: str, name: str) -> SessionRecord:
        """以事务更新名称和更新时间，避免半完成写入。"""
        validated_name = validate_session_name(name)
        updated_at = self._clock()
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
                    (validated_name, updated_at, session_id),
                )
                if cursor.rowcount != 1:
                    raise SessionError("找不到会话")
        except sqlite3.Error as error:
            raise SessionError("会话重命名失败") from error
        return self.get(session_id)

    def update_configuration(self, session_id: str, provider: str, model: str) -> SessionRecord:
        """原子更新会话模型元数据，供运行时完成成功后的模型切换。"""
        if not isinstance(provider, str) or not provider.strip():
            raise SessionError("Provider 不能为空")
        if not isinstance(model, str) or not model.strip():
            raise SessionError("模型不能为空")
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "UPDATE sessions SET provider = ?, model = ?, updated_at = ? WHERE id = ?",
                    (provider.strip(), model.strip(), self._clock(), session_id),
                )
                if cursor.rowcount != 1:
                    raise SessionError("找不到会话")
        except sqlite3.Error as error:
            raise SessionError("会话模型更新失败") from error
        return self.get(session_id)

    def load_memory(self, session_id: str) -> SessionMemory:
        """读取独立会话记忆，并拒绝损坏或类型错误的 JSON。"""
        row = self._fetchone(
            """
            SELECT summary, requirements_summary, last_task_summary,
                   modified_files_json, verification, permission
            FROM session_memory WHERE session_id = ?
            """,
            (session_id,),
        )
        if row is None:
            raise SessionError("找不到会话记忆")
        try:
            files = json.loads(_require_text(row, "modified_files_json"))
        except (TypeError, ValueError) as error:
            raise SessionError("会话记忆数据损坏") from error
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise SessionError("会话记忆数据损坏")
        return SessionMemory(
            summary=_require_text(row, "summary"),
            requirements_summary=_require_text(row, "requirements_summary"),
            last_task_summary=_require_text(row, "last_task_summary"),
            modified_files=tuple(files),
            verification=_require_text(row, "verification"),
            permission_level=_require_text(row, "permission"),
        )

    def save_memory(self, session_id: str, memory: SessionMemory) -> None:
        """原子保存安全摘要和结构化状态，不接受非字符串文件路径。"""
        if not all(isinstance(item, str) for item in memory.modified_files):
            raise SessionError("修改文件列表必须只包含文本路径")
        files_json = json.dumps(list(memory.modified_files), ensure_ascii=False)
        updated_at = self._clock()
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE session_memory
                    SET summary = ?, requirements_summary = ?, last_task_summary = ?,
                        modified_files_json = ?, verification = ?, permission = ?
                    WHERE session_id = ?
                    """,
                    (
                        memory.summary,
                        memory.requirements_summary,
                        memory.last_task_summary,
                        files_json,
                        memory.verification,
                        memory.permission_level,
                        session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SessionError("找不到会话记忆")
                connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (updated_at, session_id))
        except sqlite3.Error as error:
            raise SessionError("会话记忆保存失败") from error

    def clear_memory(self, session_id: str) -> None:
        """仅重置当前会话的摘要记忆，保留会话元数据。"""
        self.save_memory(session_id, SessionMemory())

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _fetchone(self, query: str, parameters: tuple[str, ...]) -> sqlite3.Row | None:
        try:
            with self._connection() as connection:
                return connection.execute(query, parameters).fetchone()
        except sqlite3.Error as error:
            raise SessionError("会话读取失败") from error

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=_require_text(row, "id"),
            name=_require_text(row, "name"),
            workspace=Path(_require_text(row, "workspace")),
            provider=_require_text(row, "provider"),
            model=_require_text(row, "model"),
            created_at=_require_text(row, "created_at"),
            updated_at=_require_text(row, "updated_at"),
        )
