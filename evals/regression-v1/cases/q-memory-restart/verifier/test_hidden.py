import ast
import unittest
from pathlib import Path


class HiddenTest(unittest.TestCase):
    def test_function_uses_remembered_prefix_and_returns_42(self):
        path = Path("memory_target.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        self.assertTrue(functions)
        self.assertTrue(all(name.startswith("tri_") for name in functions))
        namespace = {}
        exec(compile(tree, str(path), "exec"), namespace)
        self.assertTrue(any(namespace[name]() == 42 for name in functions))


if __name__ == "__main__":
    unittest.main()
