"""第二轮目录迁移的冷导入与轻量包边界。"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

LEGACY_MODULES = {
    "tricoder.subprocess_control",
    "tricoder.subprocess_env",
    "tricoder.workspace_lock",
    "tricoder.verification",
    "tricoder.workspace_snapshot",
    "tricoder.workspace_gate",
    "tricoder.session_lock",
    "tricoder.sessions",
    "tricoder.session_runtime",
    "tricoder.commands",
    "tricoder.approval_wait",
    "tricoder.ui",
    "tricoder.shell",
    "tricoder.tui",
}

LEGACY_FILES = {
    SRC / "tricoder" / f"{name.rsplit('.', 1)[-1]}.py"
    for name in LEGACY_MODULES
}


def _legacy_import_violations(
    source: str,
    *,
    filename: str,
    package: str,
) -> list[str]:
    """识别绝对、相对、包级和常见动态形式的旧入口导入。"""

    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    def record(node: ast.AST, name: str) -> None:
        if any(name == legacy or name.startswith(f"{legacy}.") for legacy in LEGACY_MODULES):
            violations.append(f"{getattr(node, 'lineno', 0)}:{name}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(node, alias.name)
            continue
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                try:
                    base = resolve_name(f"{'.' * node.level}{module}", package)
                except (ImportError, ValueError):
                    base = ""
            else:
                base = module
            if base:
                record(node, base)
                for alias in node.names:
                    record(node, f"{base}.{alias.name}")
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            record(node, first.value)
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"):
            continue
        imported = first.value
        if imported.startswith("."):
            package_value: str | None = None
            if len(node.args) >= 2:
                package_node = node.args[1]
                if isinstance(package_node, ast.Constant) and isinstance(
                    package_node.value,
                    str,
                ):
                    package_value = package_node.value
                elif isinstance(package_node, ast.Name) and package_node.id == "__package__":
                    package_value = package
            if package_value is None:
                for keyword in node.keywords:
                    if (
                        keyword.arg == "package"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        package_value = keyword.value.value
                        break
                    if (
                        keyword.arg == "package"
                        and isinstance(keyword.value, ast.Name)
                        and keyword.value.id == "__package__"
                    ):
                        package_value = package
                        break
            if package_value is not None:
                try:
                    imported = resolve_name(imported, package_value)
                except (ImportError, ValueError):
                    pass
        record(node, imported)

    return violations


class ModuleImportBoundaryTests(unittest.TestCase):
    def _run_isolated(self, source: str) -> subprocess.CompletedProcess[str]:
        root = ROOT
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-c", source],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_process_package_is_lightweight_in_fresh_interpreter(self) -> None:
        completed = self._run_isolated(
            "import sys; import tricoder.process; "
            "assert 'tricoder.process.control' not in sys.modules; "
            "assert 'tricoder.process.env' not in sys.modules; "
            "assert 'tricoder.session_runtime' not in sys.modules; "
            "assert 'tricoder.tui' not in sys.modules"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_all_new_and_legacy_submodules_cold_import(self) -> None:
        pairs = (
            ("tricoder.subprocess_control", "tricoder.process.control", "run_bounded_process"),
            ("tricoder.subprocess_env", "tricoder.process.env", "filtered_subprocess_env"),
            ("tricoder.workspace_lock", "tricoder.workspace.lock", "WorkspaceLock"),
            ("tricoder.verification", "tricoder.workspace.verification", "VerificationScope"),
            ("tricoder.workspace_snapshot", "tricoder.workspace.snapshot", "WorkspaceBaseline"),
            ("tricoder.workspace_gate", "tricoder.workspace.gate", "WorkspaceGate"),
            ("tricoder.session_lock", "tricoder.session.lock", "SessionLock"),
            ("tricoder.sessions", "tricoder.session.store", "SessionStore"),
            ("tricoder.session_runtime", "tricoder.session.runtime", "SessionRuntime"),
            ("tricoder.commands", "tricoder.presentation.commands", "parse_command"),
            ("tricoder.approval_wait", "tricoder.presentation.approval_wait", "ApprovalWait"),
            ("tricoder.ui", "tricoder.presentation.console", "TerminalUI"),
            ("tricoder.shell", "tricoder.presentation.shell", "InteractiveShell"),
            ("tricoder.tui", "tricoder.presentation.tui", "TricoderApp"),
        )
        source = (
            "import importlib; "
            f"pairs={pairs!r}; "
            "mods=[(importlib.import_module(old), importlib.import_module(new), symbol) "
            "for old,new,symbol in pairs]; "
            "assert all(getattr(old,symbol) is getattr(new,symbol) "
            "for old,new,symbol in mods)"
        )
        completed = self._run_isolated(source)

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_legacy_files_are_thin_forwarders(self) -> None:
        for path in sorted(LEGACY_FILES):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                definitions = [
                    node.name
                    for node in tree.body
                    if isinstance(
                        node,
                        (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                ]
                self.assertEqual([], definitions)

    def test_production_modules_do_not_import_legacy_forwarders(self) -> None:
        violations: list[str] = []
        for path in (SRC / "tricoder").rglob("*.py"):
            if path in LEGACY_FILES:
                continue
            relative = path.relative_to(SRC).with_suffix("")
            package = ".".join(relative.parts[:-1])
            for item in _legacy_import_violations(
                path.read_text(encoding="utf-8-sig"),
                filename=str(path),
                package=package,
            ):
                violations.append(f"{path.relative_to(SRC).as_posix()}:{item}")

        self.assertEqual([], violations)

    def test_legacy_import_guard_catches_relative_and_dynamic_forms(self) -> None:
        samples = (
            ("import tricoder.session_runtime", "tricoder.engine"),
            ("from tricoder import session_runtime", "tricoder.engine"),
            ("from . import session_runtime", "tricoder"),
            ("from .session_runtime import SessionRuntime", "tricoder"),
            ("from .. import session_runtime", "tricoder.engine"),
            ("from ..session_runtime import SessionRuntime", "tricoder.engine"),
            (
                "import importlib; importlib.import_module('.session_runtime', 'tricoder')",
                "tricoder.engine",
            ),
            (
                "import importlib; importlib.import_module('.session_runtime', __package__)",
                "tricoder",
            ),
            (
                "import importlib; importlib.import_module("
                "'..session_runtime', package=__package__)",
                "tricoder.engine",
            ),
        )
        for source, package in samples:
            with self.subTest(source=source, package=package):
                self.assertTrue(
                    _legacy_import_violations(
                        source,
                        filename="synthetic.py",
                        package=package,
                    )
                )

    def test_workspace_tools_binding_dependency_is_a_scoped_windows_exception(self) -> None:
        path = SRC / "tricoder" / "workspace" / "verification.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_level = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                any(
                    alias.name == "tricoder.tools"
                    or alias.name.startswith("tricoder.tools.")
                    for alias in node.names
                )
                if isinstance(node, ast.Import)
                else (node.module or "").startswith("tricoder.tools")
            )
        ]
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_bound_directory"
        )
        scoped = [
            node
            for node in ast.walk(helper)
            if isinstance(node, ast.ImportFrom)
            and node.module == "tricoder.tools.binding"
        ]

        self.assertEqual([], top_level)
        self.assertEqual(1, len(scoped))

    def test_setuptools_discovers_new_packages(self) -> None:
        from setuptools import find_packages

        packages = set(find_packages(where=str(SRC)))
        self.assertTrue(
            {
                "tricoder.process",
                "tricoder.workspace",
                "tricoder.session",
                "tricoder.presentation",
            }.issubset(packages)
        )

    def test_process_new_and_legacy_entries_cold_import(self) -> None:
        completed = self._run_isolated(
            "from tricoder.process.control import run_bounded_process as new; "
            "from tricoder.subprocess_control import run_bounded_process as old; "
            "assert old is new"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_workspace_package_is_lightweight_in_fresh_interpreter(self) -> None:
        completed = self._run_isolated(
            "import sys; import tricoder.workspace; "
            "assert 'tricoder.workspace.gate' not in sys.modules; "
            "assert 'tricoder.agent' not in sys.modules; "
            "assert 'tricoder.session_runtime' not in sys.modules; "
            "assert 'tricoder.tui' not in sys.modules"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_workspace_import_orders_are_cold_safe(self) -> None:
        snippets = (
            "import tricoder.models, tricoder.policy; "
            "from tricoder.workspace.gate import WorkspaceGate; "
            "assert WorkspaceGate.__module__ == 'tricoder.workspace.gate'",
            "from tricoder.workspace.gate import WorkspaceGate; "
            "import tricoder.tools, tricoder.agent; "
            "from tricoder.session_runtime import SessionRuntime; "
            "assert WorkspaceGate and SessionRuntime",
        )
        for source in snippets:
            with self.subTest(source=source):
                completed = self._run_isolated(source)
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_session_package_and_store_are_lightweight(self) -> None:
        snippets = (
            "import sys; import tricoder.session; "
            "assert 'tricoder.session.runtime' not in sys.modules; "
            "assert 'tricoder.agent' not in sys.modules; "
            "assert 'tricoder.presentation' not in sys.modules",
            "import sys; from tricoder.session.store import SessionStore; "
            "assert SessionStore; "
            "assert 'tricoder.session.runtime' not in sys.modules; "
            "assert 'tricoder.agent' not in sys.modules; "
            "assert 'tricoder.tui' not in sys.modules",
        )
        for source in snippets:
            with self.subTest(source=source):
                completed = self._run_isolated(source)
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_session_new_and_legacy_entries_cold_import(self) -> None:
        completed = self._run_isolated(
            "from tricoder.session.runtime import SessionRuntime as new; "
            "from tricoder.session_runtime import SessionRuntime as old; "
            "assert old is new"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_presentation_package_is_lightweight(self) -> None:
        completed = self._run_isolated(
            "import sys; import tricoder.presentation; "
            "assert 'tricoder.presentation.tui' not in sys.modules; "
            "assert 'textual' not in sys.modules; "
            "assert 'tricoder.session.runtime' not in sys.modules"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_presentation_new_and_legacy_entries_cold_import(self) -> None:
        completed = self._run_isolated(
            "from tricoder.presentation.console import TerminalUI as new; "
            "from tricoder.ui import TerminalUI as old; "
            "assert old is new"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
