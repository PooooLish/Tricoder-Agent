import math
import os
import subprocess
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
        task: str = "Repair app.py and run its tests.",
        max_rounds: int = 8,
        max_context_chars: int = 40000,
        verification_timeout: int = 30,
        verification_count: int = 1,
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
            definition = [
                f'id = "{case_id}"',
                'title = "Fix the example"',
                f'task = "{task}"',
                f"allowed_changes = {list(allowed_changes)!r}",
                f"required_changes = {list(required_changes)!r}",
                f"max_rounds = {max_rounds}",
                f"max_context_chars = {max_context_chars}",
                "",
            ]
            for verification_index in range(verification_count):
                verification_name = (
                    "unit"
                    if verification_count == 1
                    else f"unit-{verification_index}"
                )
                definition.extend(
                    (
                        "[[verification]]",
                        f'name = "{verification_name}"',
                        f'command = "{verification_command}"',
                        f"timeout = {verification_timeout}",
                        "",
                    )
                )
            (case_dir / "case.toml").write_text(
                "\n".join(definition),
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

    @unittest.skipUnless(os.name == "nt", "仅 Windows 支持 junction reparse point")
    def test_load_suite_rejects_nested_junction_in_unselected_case(self) -> None:
        """Every declared fixture tree must be safe before case filtering."""
        suite_dir = self._write_suite(case_ids=("fix-one", "fix-two"))
        outside = self.root / "outside-verifier"
        outside.mkdir()
        junction = suite_dir / "cases" / "fix-two" / "verifier" / "escape"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("当前账户不能创建 junction")

        with self.assertRaisesRegex(EvalDefinitionError, "链接|reparse"):
            load_suite(suite_dir, case_id="fix-one")

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

    def test_load_suite_rejects_sensitive_workspace_and_verifier_entries(self) -> None:
        """No credential-shaped fixture may reach an Agent or hidden verifier."""
        entries = (
            ("workspace", ".env.local"),
            ("workspace", ".envrc"),
            ("workspace", ".git/config"),
            ("workspace", "config/credentials.json"),
            ("verifier", "nested/secrets/token.txt"),
            ("verifier", "keys/private.pem"),
        )
        for fixture_name, relative in entries:
            with self.subTest(fixture=fixture_name, relative=relative):
                suite_dir = self._write_suite()
                path = (
                    suite_dir
                    / "cases"
                    / "fix-one"
                    / fixture_name
                    / relative
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic-sensitive-fixture", encoding="utf-8")

                with self.assertRaisesRegex(EvalDefinitionError, "敏感"):
                    load_suite(suite_dir)

    def test_load_suite_allows_env_example_in_both_fixture_trees(self) -> None:
        """The committed placeholder template is not a credential fixture."""
        suite_dir = self._write_suite()
        case_dir = suite_dir / "cases" / "fix-one"
        for fixture_name in ("workspace", "verifier"):
            (case_dir / fixture_name / ".env.example").write_text(
                "OPENAI_API_KEY=placeholder\n",
                encoding="utf-8",
            )

        suite = load_suite(suite_dir)

        self.assertEqual(("fix-one",), tuple(case.id for case in suite.cases))

    def test_load_suite_rejects_reserved_path_at_any_workspace_depth(self) -> None:
        """The framework verifier name is forbidden throughout Agent fixtures."""
        suite_dir = self._write_suite()
        reserved = (
            suite_dir
            / "cases"
            / "fix-one"
            / "workspace"
            / "nested"
            / ".tricoder_eval_verifier"
        )
        reserved.mkdir(parents=True)
        (reserved / "forged.py").write_text("pass\n", encoding="utf-8")

        with self.assertRaisesRegex(EvalDefinitionError, "保留"):
            load_suite(suite_dir)

    def test_load_suite_accepts_exact_fixture_entry_and_depth_limits(self) -> None:
        """Exact entry and depth budgets remain usable by legitimate fixtures."""
        entries_suite = self._write_suite()
        workspace = entries_suite / "cases" / "fix-one" / "workspace"
        for index in range(511):
            (workspace / f"empty-{index}").mkdir()

        loaded = load_suite(entries_suite)

        self.assertEqual(("fix-one",), tuple(case.id for case in loaded.cases))

        depth_suite = self._write_suite()
        current = depth_suite / "cases" / "fix-one" / "workspace"
        for _index in range(32):
            current = current / "d"
            current.mkdir()

        loaded = load_suite(depth_suite)

        self.assertEqual(("fix-one",), tuple(case.id for case in loaded.cases))

    def test_load_suite_rejects_fixture_entry_and_depth_limit_plus_one(self) -> None:
        """Large empty trees and over-deep trees fail with the fixed prefix."""
        entries_suite = self._write_suite()
        workspace = entries_suite / "cases" / "fix-one" / "workspace"
        for index in range(512):
            (workspace / f"empty-{index}").mkdir()
        with self.assertRaisesRegex(EvalDefinitionError, "评测定义超过资源上限"):
            load_suite(entries_suite)

        depth_suite = self._write_suite()
        current = depth_suite / "cases" / "fix-one" / "workspace"
        for _index in range(33):
            current = current / "d"
            current.mkdir()
        with self.assertRaisesRegex(EvalDefinitionError, "评测定义超过资源上限"):
            load_suite(depth_suite)

    def test_load_suite_accepts_conservative_resource_boundaries(self) -> None:
        """The exact documented caps must remain usable."""
        command = "python app.py " + "x" * (2048 - len("python app.py "))
        suite_dir = self._write_suite(
            case_ids=tuple(f"case-{index}" for index in range(32)),
            allowed_changes=("a" * 256,),
            required_changes=("a" * 256,),
            task="x" * 8000,
            max_rounds=64,
            max_context_chars=200_000,
            verification_command=command,
            verification_timeout=300,
            verification_count=8,
        )
        fixture = suite_dir / "cases" / "case-0" / "workspace"
        for index in range(256):
            (fixture / f"file-{index}.txt").write_bytes(b"")

        suite = load_suite(suite_dir)

        self.assertEqual(32, len(suite.cases))
        self.assertEqual(8, len(suite.cases[0].verifications))

    def test_load_suite_rejects_resource_limit_overflow(self) -> None:
        """Every untrusted definition and fixture dimension has a hard cap."""
        builders = {
            "cases": lambda: self._write_suite(
                case_ids=tuple(f"case-{index}" for index in range(33)),
            ),
            "verifications": lambda: self._write_suite(verification_count=9),
            "task": lambda: self._write_suite(task="x" * 8001),
            "command": lambda: self._write_suite(
                verification_command="python app.py " + "x" * 2035,
            ),
            "pattern": lambda: self._write_suite(allowed_changes=("a" * 257,)),
            "rounds": lambda: self._write_suite(max_rounds=65),
            "context": lambda: self._write_suite(max_context_chars=200_001),
            "timeout": lambda: self._write_suite(verification_timeout=301),
        }
        for label, builder in builders.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(EvalDefinitionError, "资源上限"):
                    load_suite(builder())

        too_many_files = self._write_suite()
        workspace = too_many_files / "cases" / "fix-one" / "workspace"
        for index in range(257):
            (workspace / f"file-{index}.txt").write_bytes(b"")
        with self.assertRaisesRegex(EvalDefinitionError, "资源上限"):
            load_suite(too_many_files)

        too_many_bytes = self._write_suite()
        (too_many_bytes / "cases" / "fix-one" / "workspace" / "large.bin").write_bytes(
            b"x" * 1_000_001
        )
        with self.assertRaisesRegex(EvalDefinitionError, "资源上限"):
            load_suite(too_many_bytes)


if __name__ == "__main__":
    unittest.main()
