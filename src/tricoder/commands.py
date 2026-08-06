"""本地斜杠命令的纯解析逻辑与命令注册表。"""

from dataclasses import dataclass


class CommandError(ValueError):
    """表示用户输入不符合本地命令语法。"""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """保存已规范化的命令与可选参数。"""

    name: str
    subcommand: str | None
    argument: str | None


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """一条本地斜杠命令的元数据；新增命令只需在此注册。"""

    description: str
    needs_confirmation: bool = False
    takes_argument: bool = False


_COMMAND_SPECS: dict[str, CommandSpec] = {
    "help": CommandSpec("显示命令、参数和示例"),
    "status": CommandSpec("显示当前 Session、工作区、Provider、模型、只读、验证及上下文状态"),
    "model": CommandSpec("显示 OpenAI、DeepSeek、GLM 的模型并按序号切换"),
    "clear": CommandSpec(
        "仅在输入 y 或 yes 后清除当前 Session 的运行时上下文和持久化摘要",
        needs_confirmation=True,
    ),
    "diff": CommandSpec("本地展示当前 Session 最近一次非空任务的正向 unified diff"),
    "undo": CommandSpec(
        "先预览反向 diff，仅在输入 y 或 yes 后撤销最近任务",
        needs_confirmation=True,
    ),
    "session": CommandSpec("列出、新建、查看或重命名会话"),
    "permission": CommandSpec(
        "查看权限级别，或切换 strict / relaxed（relaxed 自动放行只读/测试命令）",
        takes_argument=True,
    ),
    "exit": CommandSpec("保存安全记忆并退出"),
}

_SESSION_SUBCOMMANDS = {"new", "current", "rename"}
# 简单命令集合由注册表派生；session/permission 是带参数命令。
_SIMPLE_COMMANDS = frozenset(
    name for name, spec in _COMMAND_SPECS.items() if name not in {"session", "permission"}
)


def list_commands() -> dict[str, CommandSpec]:
    """返回命令注册表的只读快照，供 UI 生成帮助与校验。"""
    return dict(_COMMAND_SPECS)


def command_spec(name: str) -> CommandSpec | None:
    """按规范化名称返回命令元数据。"""
    return _COMMAND_SPECS.get(name)


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

    if name == "permission":
        argument = remainder.strip().lower() or None
        if argument is not None and argument not in {"strict", "relaxed"}:
            raise CommandError("permission 只能是 strict 或 relaxed")
        return ParsedCommand("permission", None, argument)

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
