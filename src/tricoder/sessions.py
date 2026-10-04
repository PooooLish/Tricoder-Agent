"""兼容入口；Session 存储已迁至 :mod:`tricoder.session.store`。"""

from tricoder.session.store import (
    SessionError,
    SessionStore,
    _is_windows,
    default_sessions_db,
    safe_requirement_summary,
    safe_result_summary,
    validate_session_name,
)

__all__ = [
    "SessionError",
    "SessionStore",
    "_is_windows",
    "default_sessions_db",
    "safe_requirement_summary",
    "safe_result_summary",
    "validate_session_name",
]
