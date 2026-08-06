import unittest

from tricoder.commands import (
    CommandError,
    ParsedCommand,
    command_spec,
    is_slash_command,
    list_commands,
    parse_command,
)


class CommandParserTests(unittest.TestCase):
    def test_parses_session_commands_without_losing_spaces(self) -> None:
        """防止会话名称中的大小写或内部空格在解析时丢失。"""
        self.assertEqual(
            ParsedCommand("session", "new", "认证模块 修复"),
            parse_command("/session new 认证模块 修复"),
        )
        self.assertEqual(
            ParsedCommand("session", None, None),
            parse_command("/session"),
        )

    def test_parses_all_simple_commands_case_insensitively(self) -> None:
        """防止简单命令因大小写变化被误当作未知命令。"""
        for text, name in (
            ("/help", "help"),
            ("/STATUS", "status"),
            ("  /Model", "model"),
            ("/clear", "clear"),
            ("/DIFF", "diff"),
            ("/undo", "undo"),
            ("/EXIT", "exit"),
        ):
            with self.subTest(text=text):
                self.assertEqual(ParsedCommand(name, None, None), parse_command(text))

    def test_parses_session_subcommands_case_insensitively(self) -> None:
        """防止会话子命令规范化时意外改变用户提供的会话名称。"""
        self.assertEqual(
            ParsedCommand("session", "rename", "Release Candidate"),
            parse_command("/SESSION ReNaMe Release Candidate"),
        )
        self.assertEqual(
            ParsedCommand("session", "current", None),
            parse_command("/Session CURRENT"),
        )

    def test_rejects_unknown_and_extra_arguments(self) -> None:
        """防止无效命令或多余参数进入后续 Shell 与 Provider 流程。"""
        for text in (
            "/unknown",
            "/help extra",
            "/exit now",
            "/diff extra",
            "/undo now",
            "/session bad",
        ):
            with self.subTest(text=text):
                with self.assertRaises(CommandError):
                    parse_command(text)

    def test_rejects_missing_session_names_and_current_arguments(self) -> None:
        """防止缺失名称或 current 的额外参数造成含义不明确的会话操作。"""
        for text in (
            "/session new",
            "/session new   ",
            "/session rename",
            "/session rename   ",
            "/session current extra",
        ):
            with self.subTest(text=text):
                with self.assertRaises(CommandError):
                    parse_command(text)

    def test_rejects_non_slash_text(self) -> None:
        """防止普通用户提示词被命令解析器错误接管。"""
        with self.assertRaises(CommandError):
            parse_command("请解释 /status")

    def test_rejects_slashes_without_a_command_name(self) -> None:
        """防止裸斜杠被错误解析成可执行的本地命令。"""
        for text in ("/", "/   "):
            with self.subTest(text=text):
                with self.assertRaises(CommandError):
                    parse_command(text)

    def test_is_slash_command_only_accepts_first_non_space_character(self) -> None:
        """防止提示词中的斜杠片段被误判为本地命令。"""
        self.assertTrue(is_slash_command("  /status"))
        self.assertFalse(is_slash_command("请解释 /status"))
        self.assertFalse(is_slash_command(""))

    def test_command_registry_covers_parsed_commands(self) -> None:
        """注册表必须包含全部可解析命令，且解析器只接受已注册命令。"""
        specs = list_commands()
        self.assertIn("help", specs)
        self.assertIn("session", specs)
        for name in ("help", "status", "model", "clear", "diff", "undo", "exit"):
            self.assertEqual(name, parse_command(f"/{name}").name)
        self.assertIsNotNone(command_spec("undo"))
        self.assertIsNone(command_spec("not-a-command"))
        # 破坏性命令在注册表中标记需要确认
        self.assertTrue(specs["clear"].needs_confirmation)
        self.assertTrue(specs["undo"].needs_confirmation)
        self.assertFalse(specs["status"].needs_confirmation)


if __name__ == "__main__":
    unittest.main()
