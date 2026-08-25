"""Shared credential filtering for child-process environments."""

from __future__ import annotations

from collections.abc import Mapping
import os
import re


_SENSITIVE_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|"
    r"token|password|passwd|secret|credential|authorization)"
)
_PRESERVED_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "COMSPEC",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "LC_ALL",
        "LANG",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "TERM",
        "COLORTERM",
    }
)


def filtered_subprocess_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment while removing variables likely to hold credentials."""

    env = dict(os.environ if source is None else source)
    for name in list(env):
        if name in _PRESERVED_ENV:
            continue
        if _SENSITIVE_ENV_RE.search(name):
            del env[name]
    return env
