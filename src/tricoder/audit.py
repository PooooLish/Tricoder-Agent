"""运行轨迹的脱敏与 JSONL 持久化。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SECRET_MARKERS = ("api_key", "apikey", "token", "password", "authorization", "secret")
_FREE_TEXT_KEYS = {
    "command",
    "content",
    "error",
    "message",
    "new_text",
    "old_text",
    "output",
    "query",
    "reason",
    "summary",
}


class AuditError(OSError):
    """表示审计轨迹无法可靠持久化。"""


def redact(value: Any) -> Any:
    """递归隐藏键名表明其内容可能是凭据的字段。"""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_MARKERS):
                cleaned[str(key)] = "***"
            elif lowered in _FREE_TEXT_KEYS and isinstance(item, str):
                cleaned[f"{key}_chars"] = len(item)
            else:
                cleaned[str(key)] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class AuditLogger:
    """将每个可审计事件追加为独立 JSON 对象。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def prepare(self) -> None:
        """在 Provider 或工具启动前验证目录与目标文件可追加。"""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n"):
                pass
        except OSError as exc:
            raise AuditError("无法准备可写的审计日志") from exc

    def log(self, event: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **redact(event),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                file.write("\n")
        except OSError as exc:
            raise AuditError("无法追加审计日志") from exc
