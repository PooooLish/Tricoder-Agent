"""Agent 拆分后的公开导入和内部依赖边界。"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _engine_import_violations(source: str, *, filename: str) -> list[str]:
    """识别绝对、相对和常见字符串形式的 engine 导入。"""

    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tricoder.engine" or alias.name.startswith(
                    "tricoder.engine."
                ):
                    violations.append(f"{node.lineno}:{alias.name}")
            continue
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            absolute_engine = module == "tricoder.engine" or module.startswith(
                "tricoder.engine."
            )
            relative_engine = node.level > 0 and (
                module == "engine"
                or module.startswith("engine.")
                or (not module and any(alias.name == "engine" for alias in node.names))
            )
            package_engine = module == "tricoder" and any(
                alias.name == "engine" for alias in node.names
            )
            if absolute_engine or relative_engine or package_engine:
                violations.append(
                    f"{node.lineno}:{'.' * node.level}{module or '<package>'}"
                )
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        is_import_call = (
            isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        )
        literal = node.args[0]
        if (
            is_import_call
            and isinstance(literal, ast.Constant)
            and isinstance(literal.value, str)
            and (
                literal.value == "tricoder.engine"
                or literal.value.startswith("tricoder.engine.")
                or literal.value.lstrip(".") == "engine"
                or literal.value.lstrip(".").startswith("engine.")
            )
        ):
            violations.append(f"{node.lineno}:dynamic:{literal.value}")
    return violations


class AgentImportBoundaryTests(unittest.TestCase):
    def test_public_agent_compatibility_imports(self) -> None:
        from tricoder.agent import (
            AgentObserver,
            CodingAgent,
            CONTEXT_COMPACTION_NOTICE,
            LEGACY_SYSTEM_PROMPT,
            NullObserver,
            PLANNING_PROMPT,
            compact_messages,
            compact_session_messages,
            parse_action,
        )

        for value in (
            AgentObserver,
            CodingAgent,
            CONTEXT_COMPACTION_NOTICE,
            LEGACY_SYSTEM_PROMPT,
            NullObserver,
            PLANNING_PROMPT,
            compact_messages,
            compact_session_messages,
            parse_action,
        ):
            self.assertIsNotNone(value)

    def test_engine_and_new_context_modules_do_not_import_host_or_ui(self) -> None:
        forbidden = {
            "tricoder.agent",
            "tricoder.session_runtime",
            "tricoder.session.runtime",
            "tricoder.cli",
            "tricoder.tui",
            "tricoder.presentation",
        }
        files = [*(SRC / "tricoder" / "engine").glob("*.py")]
        files.extend(
            [
                SRC / "tricoder" / "context" / "history.py",
                SRC / "tricoder" / "context" / "coordinator.py",
            ]
        )
        violations: list[str] = []
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if any(name == item or name.startswith(f"{item}.") for item in forbidden):
                        violations.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual([], violations)

    def test_memory_coordinator_does_not_import_execution_engine(self) -> None:
        path = SRC / "tricoder" / "context" / "coordinator.py"
        violations = _engine_import_violations(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        self.assertEqual([], violations)

    def test_engine_import_guard_catches_absolute_relative_and_dynamic_forms(self) -> None:
        samples = (
            "import tricoder.engine.state",
            "from tricoder.engine import state",
            "from tricoder import engine",
            "from ..engine import state",
            "from .. import engine",
            "importlib.import_module('tricoder.engine.state')",
            "__import__('..engine.state')",
        )
        for source in samples:
            with self.subTest(source=source):
                self.assertTrue(
                    _engine_import_violations(source, filename="synthetic.py")
                )

    def test_supported_modules_import_in_both_orders_in_fresh_processes(self) -> None:
        orders = (
            (
                "tricoder.agent",
                "tricoder.tools",
                "tricoder.mcp.tool_adapter",
                "tricoder.session.runtime",
                "tricoder.session_runtime",
            ),
            (
                "tricoder.session_runtime",
                "tricoder.session.runtime",
                "tricoder.mcp.tool_adapter",
                "tricoder.tools",
                "tricoder.agent",
            ),
        )
        for order in orders:
            with self.subTest(order=order):
                code = (
                    "import importlib,sys;"
                    f"sys.path.insert(0,{str(SRC)!r});"
                    f"mods={order!r};"
                    "[importlib.import_module(name) for name in mods]"
                )
                completed = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
