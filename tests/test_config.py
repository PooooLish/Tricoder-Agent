import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricoder import config as config_module
from tricoder.config import ConfigError, load_config, preview_provider_models


class ConfigTests(unittest.TestCase):
    def test_preview_provider_models_uses_project_models_without_keys(self) -> None:
        """模型列表不得为了展示而读取 .env.local 或要求任一 Provider Key。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                "[providers.openai]\nmodel = \"openai-project\"\n"
                "[providers.deepseek]\nmodel = \"deepseek-project\"\n"
                "[providers.glm]\nmodel = \"glm-project\"\n",
                encoding="utf-8",
            )

            models = preview_provider_models(workspace, environ={})

        self.assertEqual(
            {
                "openai": "openai-project",
                "deepseek": "deepseek-project",
                "glm": "glm-project",
            },
            models,
        )

    def test_default_audit_dir_uses_localappdata_on_windows(self) -> None:
        """Windows 默认审计目录应位于受控的本地应用数据目录。"""
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory).resolve() / "local-app-data"
            default_directory = getattr(
                config_module,
                "default_audit_dir",
                lambda _environ: None,
            )

            with patch.object(
                config_module,
                "_is_windows",
                return_value=True,
                create=True,
            ):
                audit_dir = default_directory(
                    {"LOCALAPPDATA": str(local_app_data)}
                )

            self.assertEqual(local_app_data / "TriCoder" / "runs", audit_dir)
            self.assertTrue(audit_dir.is_absolute())

    def test_default_audit_dir_uses_xdg_state_home_off_windows(self) -> None:
        """非 Windows 默认审计目录应遵循受控的 XDG 状态目录。"""
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory).resolve() / "state"
            default_directory = getattr(
                config_module,
                "default_audit_dir",
                lambda _environ: None,
            )

            with patch.object(
                config_module,
                "_is_windows",
                return_value=False,
                create=True,
            ):
                audit_dir = default_directory({"XDG_STATE_HOME": str(state_home)})

            self.assertEqual(state_home / "tricoder" / "runs", audit_dir)
            self.assertTrue(audit_dir.is_absolute())

    def test_default_audit_dir_uses_local_state_fallback_off_windows(self) -> None:
        """未配置 XDG_STATE_HOME 时只组合受控 Home 下的 runs 路径。"""
        with tempfile.TemporaryDirectory() as directory:
            controlled_home = Path(directory).resolve() / "home"
            with (
                patch.object(config_module, "_is_windows", return_value=False),
                patch.object(config_module.Path, "home", return_value=controlled_home),
            ):
                audit_dir = config_module.default_audit_dir({})

            self.assertEqual(
                controlled_home / ".local" / "state" / "tricoder" / "runs",
                audit_dir,
            )

    def test_load_config_supplies_an_absolute_default_audit_dir(self) -> None:
        """运行配置必须始终包含已解析的默认审计目录。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve() / "workspace"
            workspace.mkdir()
            local_app_data = Path(directory).resolve() / "local-app-data"

            with patch.object(config_module, "_is_windows", return_value=True):
                config = load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={
                        "OPENAI_API_KEY": "test-key",
                        "LOCALAPPDATA": str(local_app_data),
                    },
                )

            self.assertEqual(
                local_app_data / "TriCoder" / "runs",
                getattr(config, "audit_dir", None),
            )
            self.assertTrue(config.audit_dir.is_absolute())

    def test_load_config_resolves_relative_audit_dir_from_process_cwd(self) -> None:
        """显式相对审计目录必须相对于调用进程目录，而非目标工作区。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            process_cwd = root / "launcher"
            workspace.mkdir()
            process_cwd.mkdir()
            original_cwd = Path.cwd()
            try:
                os.chdir(process_cwd)
                try:
                    config = load_config(
                        provider="openai",
                        workspace=workspace,
                        environ={"OPENAI_API_KEY": "test-key"},
                        audit_dir=Path("audit-output"),
                    )
                except TypeError:
                    config = None
            finally:
                os.chdir(original_cwd)

            self.assertEqual(
                process_cwd / "audit-output",
                getattr(config, "audit_dir", None),
            )

    def test_read_only_rejects_audit_dir_inside_workspace(self) -> None:
        """只读模式不得把审计目录设为目标工作区或其子目录。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve() / "workspace"
            workspace.mkdir()

            for audit_dir in (workspace, workspace / "audit"):
                with self.subTest(audit_dir=audit_dir):
                    with self.assertRaisesRegex(ConfigError, "只读模式"):
                        load_config(
                            provider="openai",
                            workspace=workspace,
                            environ={"OPENAI_API_KEY": "test-key"},
                            audit_dir=audit_dir,
                            read_only=True,
                        )

    def test_read_only_allows_controlled_default_audit_dir_outside_workspace(self) -> None:
        """只读模式仍应允许受控的默认外部审计目录。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            local_app_data = root / "local-app-data"
            workspace.mkdir()

            with patch.object(config_module, "_is_windows", return_value=True):
                config = load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={
                        "OPENAI_API_KEY": "test-key",
                        "LOCALAPPDATA": str(local_app_data),
                    },
                    read_only=True,
                )

            self.assertEqual(local_app_data / "TriCoder" / "runs", config.audit_dir)

    def test_cli_values_override_project_config(self) -> None:
        """防止命令行显式选择被项目配置静默覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_file = workspace / ".tricoder.toml"
            config_file.write_text(
                '[agent]\nmodel = "file-model"\nmax_rounds = 7\n'
                '[providers.deepseek]\nbase_url = "https://file.example/v1"\n',
                encoding="utf-8",
            )

            config = load_config(
                provider="deepseek",
                workspace=workspace,
                environ={"DEEPSEEK_API_KEY": "test-key"},
                model="cli-model",
                base_url="https://cli.example/v1",
                max_rounds=3,
            )

            self.assertEqual("cli-model", config.provider.model)
            self.assertEqual("https://cli.example/v1", config.provider.base_url)
            self.assertEqual(3, config.max_rounds)

    def test_project_config_overrides_safe_defaults(self) -> None:
        """防止有效的项目级模型设置被内置默认值忽略。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                '[agent]\nmodel = "project-model"\ntimeout = 12\n',
                encoding="utf-8",
            )

            config = load_config(
                provider="glm",
                workspace=workspace,
                environ={"ZAI_API_KEY": "test-key"},
            )

            self.assertEqual("project-model", config.provider.model)
            self.assertEqual(12.0, config.timeout)
            self.assertEqual(
                "https://open.bigmodel.cn/api/paas/v4",
                config.provider.base_url,
            )

    def test_api_key_only_comes_from_environment(self) -> None:
        """防止配置文件中的凭据被程序读取并扩散到运行时。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                '[providers.openai]\napi_key = "must-not-be-used"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "OPENAI_API_KEY"):
                load_config(provider="openai", workspace=workspace, environ={})

    def test_local_env_file_supplies_api_key_for_local_development(self) -> None:
        """防止被 Git 忽略的本地密钥文件无法支持日常调试。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".env.local").write_text(
                "# 本地开发凭据\nDEEPSEEK_API_KEY=\"local-test-key\"\n",
                encoding="utf-8",
            )

            config = load_config(
                provider="deepseek",
                workspace=workspace,
                environ={},
            )

            self.assertEqual("local-test-key", config.provider.api_key)

    def test_process_environment_overrides_local_env_file(self) -> None:
        """防止 CI 或临时 Shell 注入的密钥被本地文件覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".env.local").write_text(
                "ZAI_API_KEY=file-test-key\n",
                encoding="utf-8",
            )

            config = load_config(
                provider="glm",
                workspace=workspace,
                environ={"ZAI_API_KEY": "process-test-key"},
            )

            self.assertEqual("process-test-key", config.provider.api_key)

    def test_explicit_env_file_can_be_outside_target_workspace(self) -> None:
        """防止测试沙盒必须复制一份真实密钥文件才能运行。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "target"
            workspace.mkdir()
            env_file = root / "shared.env.local"
            env_file.write_text("DEEPSEEK_API_KEY=shared-test-key\n", encoding="utf-8")

            config = load_config(
                provider="deepseek",
                workspace=workspace,
                environ={},
                env_file=env_file,
            )

            self.assertEqual("shared-test-key", config.provider.api_key)
            self.assertEqual(env_file.resolve(), config.env_file)

    def test_explicit_missing_env_file_is_rejected(self) -> None:
        """防止路径拼写错误悄悄回退到其他凭据来源。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            missing = workspace.parent / "missing.env.local"

            with self.assertRaisesRegex(ConfigError, "env-file.*不存在"):
                load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={"OPENAI_API_KEY": "process-test-key"},
                    env_file=missing,
                )

    def test_local_env_rejects_malformed_lines_without_echoing_value(self) -> None:
        """防止拼写错误被静默忽略，也防止错误消息泄露整行内容。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            secret_text = "must-never-appear-in-error"
            (workspace / ".env.local").write_text(
                f"OPENAI_API_KEY {secret_text}\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError) as captured:
                load_config(provider="openai", workspace=workspace, environ={})

            self.assertIn("第 1 行", str(captured.exception))
            self.assertNotIn(secret_text, str(captured.exception))

    def test_unknown_provider_is_rejected(self) -> None:
        """防止拼写错误落到错误的模型服务。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "不支持"):
                load_config(
                    provider="unknown",
                    workspace=Path(directory),
                    environ=os.environ,
                )

    def test_project_config_cannot_redirect_api_key_to_unknown_host(self) -> None:
        """防止恶意仓库配置把 API Key 静默发送到第三方地址。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                '[providers.openai]\nbase_url = "https://attacker.example/v1"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "项目配置.*官方"):
                load_config(
                    provider="openai",
                    workspace=workspace,
                    environ={"OPENAI_API_KEY": "test-key"},
                )

    def test_project_config_accepts_official_glm_coding_endpoint(self) -> None:
        """防止安全限制误伤智谱官方 Coding Plan 端点。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                '[providers.glm]\n'
                'base_url = "https://open.bigmodel.cn/api/coding/paas/v4"\n',
                encoding="utf-8",
            )

            config = load_config(
                provider="glm",
                workspace=workspace,
                environ={"ZAI_API_KEY": "test-key"},
            )

            self.assertEqual(
                "https://open.bigmodel.cn/api/coding/paas/v4",
                config.provider.base_url,
            )

    def test_max_context_chars_uses_cli_environment_toml_priority(self) -> None:
        """防止上下文预算来源的优先级与其他 Agent 配置不一致。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                "[agent]\nmax_context_chars = 300\n",
                encoding="utf-8",
            )

            config = load_config(
                provider="openai",
                workspace=workspace,
                environ={
                    "OPENAI_API_KEY": "test-key",
                    "TRICODER_MAX_CONTEXT_CHARS": "400",
                },
                max_context_chars=500,
            )

            self.assertEqual(500, config.max_context_chars)

    def test_max_context_chars_uses_environment_before_toml_and_default(self) -> None:
        """防止环境覆盖或默认预算被项目 TOML 配置意外忽略。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                "[agent]\nmax_context_chars = 300\n",
                encoding="utf-8",
            )

            from_environment = load_config(
                provider="openai",
                workspace=workspace,
                environ={
                    "OPENAI_API_KEY": "test-key",
                    "TRICODER_MAX_CONTEXT_CHARS": "400",
                },
            )
            default = load_config(
                provider="openai",
                workspace=Path(tempfile.mkdtemp()),
                environ={"OPENAI_API_KEY": "test-key"},
            )

            self.assertEqual(400, from_environment.max_context_chars)
            self.assertEqual(80_000, default.max_context_chars)

    def test_max_context_chars_rejects_non_positive_values(self) -> None:
        """防止零或负数预算导致上下文裁剪逻辑失去明确边界。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            for value in (0, -1):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ConfigError, "max_context_chars"):
                        load_config(
                            provider="openai",
                            workspace=workspace,
                            environ={"OPENAI_API_KEY": "test-key"},
                            max_context_chars=value,
                        )

    def test_max_context_chars_rejects_non_integer_values(self) -> None:
        """防止浮点数或布尔值被静默转换为看似有效的字符预算。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            for value in (1.5, True):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ConfigError, "max_context_chars"):
                        load_config(
                            provider="openai",
                            workspace=workspace,
                            environ={"OPENAI_API_KEY": "test-key"},
                            max_context_chars=value,
                        )

    def test_tool_protocol_defaults_to_native(self) -> None:
        """防止未配置协议时意外回退到旧的 JSON 兼容模式。"""
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                provider="openai",
                workspace=Path(directory),
                environ={"OPENAI_API_KEY": "test-key"},
            )

        self.assertEqual("native", config.tool_protocol)

    def test_tool_protocol_uses_environment_before_project_config(self) -> None:
        """防止环境中的兼容模式开关被项目配置静默覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".tricoder.toml").write_text(
                '[agent]\ntool_protocol = "native"\n',
                encoding="utf-8",
            )

            config = load_config(
                provider="openai",
                workspace=workspace,
                environ={
                    "OPENAI_API_KEY": "test-key",
                    "TRICODER_TOOL_PROTOCOL": "legacy_json",
                },
            )

        self.assertEqual("legacy_json", config.tool_protocol)

    def test_tool_protocol_rejects_non_exact_values(self) -> None:
        """防止大小写、空白或未知协议值被静默标准化后启用。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for value in ("NATIVE", " native", "native ", "", "unknown"):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ConfigError, "tool_protocol"):
                        load_config(
                            provider="openai",
                            workspace=workspace,
                            environ={
                                "OPENAI_API_KEY": "test-key",
                                "TRICODER_TOOL_PROTOCOL": value,
                            },
                        )


if __name__ == "__main__":
    unittest.main()
