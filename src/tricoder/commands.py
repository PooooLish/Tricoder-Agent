"""本地斜杠命令的纯解析逻辑。"""

from dataclasses import dataclass


_SIMPLE_COMMANDS = {"help", "status", "model", "clear", "exit"}
_SESSION_SUBCOMMANDS = {"new", "current", "rename"}


class CommandError(ValueError):
    """表示用户输入不符合本地命令语法。"""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """保存已规范化的命令与可选参数。"""

    name: str
    subcommand: str | None
    argument: str | None


def is_slash_command(text: str) -> bool:
    """仅当首个非空白字符为斜杠时才识别为本地命令。"""
    return text.lstrip().startswith("/")


def parse_command(text: str) -> ParsedCommand:
    """解析受支持的本地命令，绝不执行任何外部操作。"""
    source = text.lstrip()
    if not source.startswith("/"):
        raise CommandError("命令必须以 / 开头。")

    # 仅移除命令结构周围的空白，Session 名称的内部空格保持不变。
    body = source[1:].strip()
    if not body:
        raise CommandError("缺少命令名称。请输入 /help 查看可用命令。")

    command_parts = body.split(maxsplit=1)
    name = command_parts[0].lower()
    remainder = command_parts[1] if len(command_parts) == 2 else ""

    if name in _SIMPLE_COMMANDS:
        if remainder:
            raise CommandError(f"命令 /{name} 不接受参数。")
        return ParsedCommand(name, None, None)

    if name != "session":
        raise CommandError(f"未知命令：/{name}。请输入 /help 查看可用命令。")

    if not remainder:
        return ParsedCommand("session", None, None)

    session_parts = remainder.split(maxsplit=1)
    subcommand = session_parts[0].lower()
    argument = session_parts[1] if len(session_parts) == 2 else None
    if subcommand not in _SESSION_SUBCOMMANDS:
        raise CommandError(f"不支持的会话子命令：{session_parts[0]}。")

    if subcommand == "current":
        if argument is not None:
            raise CommandError("命令 /session current 不接受参数。")
        return ParsedCommand("session", "current", None)

    if argument is None or not argument.strip():
        raise CommandError(f"命令 /session {subcommand} 需要名称。")
    return ParsedCommand("session", subcommand, argument)
