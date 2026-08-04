"""工作区根 .gitignore 的基础忽略规则解析。"""

from __future__ import annotations

import fnmatch
from pathlib import Path


def _directory_prefixes(relative: str) -> list[str]:
    """返回相对路径的各级目录前缀，如 a/b/c.py -> ["a", "a/b"]。"""
    parts = relative.split("/")[:-1]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def _ignore_match(rule: str, relative: str) -> bool:
    """以简化 .gitignore 语义判断单个规则是否命中相对路径。

    尾随 `/` 的目录规则通过 basename 或目录前缀匹配任意层级。
    """
    rule = rule.rstrip("/")
    norm = relative.rstrip("/")
    if "/" not in rule:
        if fnmatch.fnmatch(norm.split("/")[-1], rule):
            return True
    else:
        candidate = rule.lstrip("/")
        if candidate.endswith("/**"):
            prefix = candidate[:-3]
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        elif fnmatch.fnmatch(norm, candidate):
            return True
    for prefix in _directory_prefixes(norm):
        if fnmatch.fnmatch(prefix, rule):
            return True
    return False


class _GitIgnoreMatcher:
    """从工作区根 .gitignore 构造基础忽略规则。

    MVP 仅支持常见模式：非空行、注释、`!` 取反（最后规则生效）、
    `*`/`**`、尾随 `/` 目录规则和根锚定 `/`。复杂语法按宽松方式近似。
    """

    def __init__(self, workspace: Path) -> None:
        self._rules: list[tuple[bool, str]] = []
        try:
            lines = (
                (workspace / ".gitignore")
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except OSError:
            return
        for line in lines:
            rule = line.strip()
            if not rule or rule.startswith("#"):
                continue
            negated = rule.startswith("!")
            if negated:
                rule = rule[1:].strip()
            if not rule:
                continue
            self._rules.append((not negated, rule))

    def is_ignored(self, relative: str) -> bool:
        ignored = False
        for is_ignore, rule in self._rules:
            if _ignore_match(rule, relative):
                ignored = is_ignore
        return ignored
