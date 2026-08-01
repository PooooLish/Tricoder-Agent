import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from tricoder.models import AppConfig, ProviderConfig, RunResult, ToolAction, ToolResult
from tricoder.models import SessionRecord
from tricoder.ui import TerminalUI


def recording_ui(*, answers: list[str] | None = None) -> tuple[TerminalUI, Console]:
    output = io.StringIO()
    console = Console(
        file=output,
        width=100,
        record=True,
        force_terminal=False,
        color_system=None,
        no_color=True,
    )
    decisions = iter(answers or [])
    return (
        TerminalUI(console=console, input_fn=lambda _prompt: next(decisions)),
        console,
    )


class TerminalUITests(unittest.TestCase):
    def test_shell_methods_render_literal_status_and_validate_choices(self) -> None:
        """Shell UI 要显示 Session 状态，并将无效编号安全地留在本地。"""
        ui, console = recording_ui(answers=["9", "2", "3", "yes", "no"])
        first = SessionRecord(
            "one", "[first]", Path("D:/one"), "openai", "model-a", "created", "updated"
        )
        second = SessionRecord(
            "two", "second", Path("D:/two"), "glm", "model-b", "created", "updated"
        )

        ui.show_shell_start(first)
        ui.show_help()
        ui.show_status(
            SimpleNamespace(record=first, unsaved_memory=True, warning="本次记忆未持久化"),
            SimpleNamespace(config=SimpleNamespace(read_only=True), context=SimpleNamespace(messages=("a",))),
        )
        self.assertIsNone(ui.choose_session([first, second], first.id))
        self.assertEqual("two", ui.choose_session([first, second], first.id))
        self.assertEqual(
            "glm",
            ui.choose_model(
                {
                    "openai": "gpt-preview",
                    "deepseek": "deepseek-preview",
                    "glm": "glm-preview",
                },
                "openai",
            ),
        )
        self.assertTrue(ui.confirm("确认切换"))
        self.assertFalse(ui.confirm("确认切换"))

        text = console.export_text()
        self.assertIn("[first]", text)
        self.assertIn("/session new", text)
        self.assertIn("/diff", text)
        self.assertIn("/undo", text)
        self.assertIn("未持久化", text)
        self.assertIn("无效", text)
        self.assertIn("gpt-preview", text)
        self.assertIn("deepseek-preview", text)
        self.assertIn("glm-preview", text)

    def test_show_diff_renders_dynamic_content_as_literal_text(self) -> None:
        """反向 diff 及路径中的 Rich 标记必须按字面显示。"""
        output = io.StringIO()
        console = Console(
            file=output,
            width=100,
            record=True,
            markup=True,
            force_terminal=False,
            color_system=None,
            no_color=True,
        )
        ui = TerminalUI(console=console)
        diff = "--- [bold red]not markup[/bold red]\n+++ src/app.py\n"

        ui.show_diff(diff, title="撤销预览")

        text = console.export_text()
        self.assertIn("撤销预览", text)
        self.assertIn("[bold red]not markup[/bold red]", text)

    def test_undo_failure_path_renders_as_literal_text(self) -> None:
        """撤销失败中的动态路径不能被 Rich 当作样式标记。"""
        output = io.StringIO()
        console = Console(
            file=output,
            width=100,
            record=True,
            markup=True,
            force_terminal=False,
            color_system=None,
            no_color=True,
        )
        ui = TerminalUI(console=console)
        path = "src/[bold red]not markup[/bold red].py"

        ui.show_error("撤销失败", f"撤销未完成且补偿失败：{path}")

        text = console.export_text()
        self.assertIn("撤销失败", text)
        self.assertIn(path, text)
        self.assertNotIn("-new", text)
    def test_start_panel_exposes_task_provider_workspace_and_mode(self) -> None:
        """防止启动界面缺少执行前最重要的上下文。"""
        ui, console = recording_ui()
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                Path(directory),
                ProviderConfig("deepseek", "secret", "https://api.deepseek.com", "model-x"),
                read_only=True,
                env_file=Path("D:/secrets/.env.local"),
                key_source=".env.local",
            )

            ui.show_start("分析一个很长但可读的任务", config)

        text = console.export_text()
        self.assertIn("TriCoder CLI", text)
        self.assertIn("分析一个很长但可读的任务", text)
        self.assertIn("DeepSeek", text)
        self.assertIn("model-x", text)
        self.assertIn("只读", text)
        self.assertIn("密钥来源", text)
        self.assertIn(".env.local", text)
        self.assertNotIn("secret", text)

    def test_dynamic_content_is_rendered_as_literal_text(self) -> None:
        """防止用户或模型文本被 Rich 当成样式标记解析。"""
        ui, console = recording_ui()
        config = AppConfig(
            Path("D:/[workspace]"),
            ProviderConfig("openai", "secret", "https://example.test", "[model]"),
        )

        ui.show_start("[bold red]保留这些括号[/bold red]", config)
        ui.show_complete(
            RunResult(True, "[green]字面摘要[/green]", 1),
            Path("runtime/[run].jsonl"),
        )

        text = console.export_text()
        self.assertIn("[bold red]保留这些括号[/bold red]", text)
        self.assertIn("[model]", text)
        self.assertIn("[green]字面摘要[/green]", text)
        self.assertIn("[run].jsonl", text)

    def test_shell_run_result_renders_dynamic_text_literally(self) -> None:
        """防止交互结果的摘要和验证文本被 Rich 当成 markup 吞掉。"""
        ui, console = recording_ui()
        self.assertTrue(hasattr(ui, "show_run_result"))

        ui.show_run_result(  # type: ignore[attr-defined]
            RunResult(
                False,
                "[bold red]字面失败摘要[/bold red]",
                2,
                modified_files=("src/app.py",),
                verification="[yellow]未通过[/yellow]",
            )
        )

        text = console.export_text()
        self.assertIn("未完成", text)
        self.assertIn("[bold red]字面失败摘要[/bold red]", text)
        self.assertIn("[yellow]未通过[/yellow]", text)
        self.assertIn("修改文件", text)
        self.assertIn("1", text)

    def test_edit_approval_renders_diff_and_defaults_to_deny(self) -> None:
        """防止审批界面隐藏修改内容或把空回答视为允许。"""
        ui, console = recording_ui(answers=[""])
        diff = "--- app.py\n+++ app.py\n@@ -1 +1 @@\n-old\n+new\n"

        allowed = ui.approve("edit_file", diff)

        self.assertFalse(allowed)
        text = console.export_text()
        self.assertIn("文件修改", text)
        self.assertIn("-old", text)
        self.assertIn("+new", text)

    def test_create_approval_renders_diff_and_uses_file_creation_title(self) -> None:
        """防止 create_file 被当成普通命令展示而失去 Diff 审阅语义。"""
        ui, console = recording_ui(answers=["yes"])
        diff = "--- /dev/null\n+++ created.py\n@@ -0,0 +1 @@\n+new\n"

        allowed = ui.approve("create_file", diff)

        self.assertTrue(allowed)
        text = console.export_text()
        self.assertIn("文件创建", text)
        self.assertIn("+new", text)

    def test_command_approval_accepts_explicit_yes(self) -> None:
        """防止 Rich 改造破坏原有的明确审批语义。"""
        ui, console = recording_ui(answers=["yes"])

        allowed = ui.approve(
            "run_command",
            "目录：D:\\demo\n命令：python -m unittest\n超时：30 秒",
        )

        self.assertTrue(allowed)
        self.assertIn("命令执行", console.export_text())

    def test_doctor_error_and_completion_have_distinct_states(self) -> None:
        """防止检查、错误和完成信息混成难以扫描的普通文本。"""
        ui, console = recording_ui()
        config = AppConfig(
            Path("D:/demo"),
            ProviderConfig("glm", "secret", "https://example.test", "glm-test"),
        )

        ui.show_doctor(config, "ZAI_API_KEY")
        ui.show_error("配置错误", "缺少变量")
        ui.show_complete(
            RunResult(
                True,
                "任务完成",
                4,
                tool_calls=6,
                modified_files=("src/app.py", "tests/test_app.py"),
                verification="通过",
            ),
            Path("runtime/runs/example.jsonl"),
        )

        text = console.export_text()
        self.assertIn("配置检查", text)
        self.assertIn("ZAI_API_KEY", text)
        self.assertIn("未发送", text)
        self.assertIn("配置错误", text)
        self.assertIn("任务完成", text)
        self.assertIn("工具调用", text)
        self.assertIn("6", text)
        self.assertIn("修改文件", text)
        self.assertIn("2", text)
        self.assertIn("验证结果", text)
        self.assertIn("通过", text)
        self.assertIn("example.jsonl", text)
        self.assertNotIn("secret", text)

    def test_doctor_redacts_base_url_credentials_query_and_fragment(self) -> None:
        """防止自定义 Base URL 把 userinfo、查询凭据或片段写入诊断输出。"""
        ui, console = recording_ui()
        config = AppConfig(
            Path("D:/demo"),
            ProviderConfig(
                "openai",
                "api-key-sentinel",
                (
                    "https://URL-USER:URL-PASSWORD@example.test:8443/v1/chat"
                    "?token=QUERY-SENTINEL#FRAGMENT-SENTINEL"
                ),
                "model-test",
            ),
        )

        ui.show_doctor(config, "OPENAI_API_KEY")

        text = console.export_text()
        self.assertIn("https://example.test:8443/v1/chat", text)
        for sentinel in (
            "URL-USER",
            "URL-PASSWORD",
            "QUERY-SENTINEL",
            "FRAGMENT-SENTINEL",
            "api-key-sentinel",
        ):
            self.assertNotIn(sentinel, text)

    def test_agent_events_render_round_action_and_result(self) -> None:
        """防止运行期间只显示空白等待，用户无法判断 Agent 在做什么。"""
        ui, console = recording_ui()
        action = ToolAction("read_file", {"path": "src/app.py"}, "查看实现")

        ui.on_round_start(2, 8)
        ui.on_action(action)
        ui.on_tool_result(action, ToolResult(True, "content"), 125)

        text = console.export_text()
        self.assertIn("第 2/8 轮", text)
        self.assertIn("read_file", text)
        self.assertIn("查看实现", text)
        self.assertIn("125 ms", text)


if __name__ == "__main__":
    unittest.main()
