import math
import tempfile
import unittest
from pathlib import Path

from tricoder.evals.loader import (
    EvalDefinitionError,
    is_reserved_eval_path,
    load_suite,
)


class EvalLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.suite_count = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_suite(
        self,
        *,
        case_ids: tuple[str, ...] = ("fix-one",),
        allowed_changes: tuple[str, ...] = ("app.py", "tests/**"),
        required_changes: tuple[str, ...] = ("app.py",),
        verification_command: str = "python -m unittest discover -s tests -q",
        include_verifier: bool = True,
    ) -> Path:
        self.suite_count += 1
        suite_dir = self.root / f"smoke-{self.suite_count}"
        suite_dir.mkdir()
        cases = ", ".join(f'"{case_id}"' for case_id in case_ids)
        (suite_dir / "suite.toml").write_text(
            f'id = "smoke"\ntitle = "TriCoder Smoke Eval"\ncases = [{cases}]\n',
            encoding="utf-8",
        )
        for case_id in set(case_ids):
            case_dir = suite_dir / "cases" / case_id
            (case_dir / "workspace" / "tests").mkdir(parents=True)
            if include_verifier:
                (case_dir / "verifier").mkdir()
            (case_dir / "case.toml").write_text(
                "\n".join(
                    (
                        f'id = "{case_id}"',
                        'title = "Fix the example"',
                        'task = "Repair app.py and run its tests."',
                        f"allowed_changes = {list(allowed_changes)!r}",
                        f"required_changes = {list(required_changes)!r}",
                        "max_rounds = 8",
                        "max_context_chars = 40000",
                        "",
                        "[[verification]]",
                        'name = "unit"',
                        f'command = "{verification_command}"',
                        "timeout = 30",
                        "",
                    )
                ),
                encoding="utf-8",
            )
        return suite_dir

    def test_load_suite_parses_and_filters_a_valid_case(self) -> None:
        """防止 Loader 忽略 suite 顺序或未按 case_id 收窄执行范围。"""
        suite_dir = self._write_suite(case_ids=("fix-one", "fix-two"))

        suite = load_suite(suite_dir, case_id="fix-two")

        self.assertEqual("smoke", suite.id)
        self.assertEqual(("fix-two",), tuple(case.id for case in suite.cases))
        self.assertEqual("unit", suite.cases[0].verifications[0].name)
        self.assertEqual((suite_dir / "cases" / "fix-two").resolve(), suite.cases[0].source_dir)

    def test_load_suite_rejects_duplicate_case_ids(self) -> None:
        """防止同一 case 被定义两次而导致结果归属不确定。"""
        suite_dir = self._write_suite(case_ids=("fix-one", "fix-one"))

        with self.assertRaisesRegex(EvalDefinitionError, "重复"):
            load_suite(suite_dir)

    def test_load_suite_rejects_missing_verifier_directory(self) -> None:
        """防止缺失隐藏验证目录的 case 被当成有效评测。"""
        suite_dir = self._write_suite(include_verifier=False)

        with self.assertRaisesRegex(EvalDefinitionError, "verifier"):
            load_suite(suite_dir)

    def test_load_suite_rejects_absolute_and_parent_globs(self) -> None:
        """防止修改范围模式越过工作副本边界。"""
        for pattern in ("/outside/**", "../outside/**"):
            with self.subTest(pattern=pattern):
                suite_dir = self._write_suite(allowed_changes=(pattern,))
                with self.assertRaisesRegex(EvalDefinitionError, "模式"):
                    load_suite(suite_dir)

    def test_load_suite_rejects_reserved_verifier_change_pattern(self) -> None:
        """防止 Agent 修改延迟注入的隐藏 verifier。"""
        for pattern in (
            ".tricoder_eval_verifier/**",
            "tests/.tricoder_eval_verifier/test_hidden.py",
        ):
            with self.subTest(pattern=pattern):
                suite_dir = self._write_suite(allowed_changes=(pattern,))
                with self.assertRaisesRegex(EvalDefinitionError, "保留目录"):
                    load_suite(suite_dir)

    def test_broad_change_patterns_stay_valid_but_reserved_paths_are_identified(self) -> None:
        """防止宽泛 workspace 模式在评分时把框架 verifier 误当成 Agent 修改。"""
        suite_dir = self._write_suite(allowed_changes=("**", "*", "**/*"))

        suite = load_suite(suite_dir)

        self.assertEqual(("**", "*", "**/*"), suite.cases[0].allowed_changes)
        self.assertTrue(is_reserved_eval_path(".tricoder_eval_verifier/test_hidden.py"))
        self.assertTrue(is_reserved_eval_path("nested/.tricoder_eval_verifier/data.py"))
        self.assertFalse(is_reserved_eval_path("app.py"))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "当前平台不支持符号链接")
    def test_load_suite_rejects_symlinked_workspace_directory(self) -> None:
        """防止 workspace fixture 通过符号链接指向 suite 外部。"""
        suite_dir = self._write_suite()
        workspace_dir = suite_dir / "cases" / "fix-one" / "workspace"
        outside = self.root / "outside-workspace"
        outside.mkdir()
        (workspace_dir / "tests").rmdir()
        workspace_dir.rmdir()
        try:
            workspace_dir.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("当前账户不能创建符号链接")

        with self.assertRaisesRegex(EvalDefinitionError, "链接|边界"):
            load_suite(suite_dir)

    def test_load_suite_rejects_non_finite_verification_timeout(self) -> None:
        """防止 NaN 或 Infinity timeout 传入 subprocess 后破坏验证限制。"""
        for timeout in (math.nan, math.inf):
            with self.subTest(timeout=timeout):
                suite_dir = self._write_suite()
                case_toml = suite_dir / "cases" / "fix-one" / "case.toml"
                case_toml.write_text(
                    case_toml.read_text(encoding="utf-8").replace(
                        "timeout = 30", f"timeout = {timeout}",
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(EvalDefinitionError, "正数"):
                    load_suite(suite_dir)

    def test_load_suite_rejects_verifier_command_with_shell_chaining(self) -> None:
        """防止验证命令通过 shell 元字符读取凭据或执行额外命令。"""
        suite_dir = self._write_suite(
            verification_command="python -m unittest -q && type .env.local",
        )

        with self.assertRaisesRegex(EvalDefinitionError, "验证命令"):
            load_suite(suite_dir)


if __name__ == "__main__":
    unittest.main()
