"""兼容入口；斜杠命令解析已迁至 :mod:`tricoder.presentation.commands`。"""

from tricoder.presentation.commands import (
    CommandError,
    CommandSpec,
    ParsedCommand,
    command_spec,
    is_slash_command,
    list_commands,
    parse_command,
)

__all__ = [
    "CommandError",
    "CommandSpec",
    "ParsedCommand",
    "command_spec",
    "is_slash_command",
    "list_commands",
    "parse_command",
]
