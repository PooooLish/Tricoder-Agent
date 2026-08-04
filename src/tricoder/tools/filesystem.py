"""工作区目录枚举与文本文件读取工具。"""

from __future__ import annotations

from typing import Any

from tricoder.models import ToolResult
from tricoder.policy import PolicyError

from tricoder.tools.handlers import ToolHandler


class ListFilesTool(ToolHandler):
    name = "list_files"
    description = "列出工作区内指定目录的条目。"
    parameters = ToolHandler._schema({"path": {"type": "string"}})

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        directory = self.context.workspace_policy.resolve_path(
            str(arguments.get("path", ".")),
        )
        if not directory.is_dir():
            return ToolResult(False, "list_files 的目标必须是目录")
        entries: list[str] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            try:
                self.context.workspace_policy.resolve_path(child)
            except PolicyError:
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.relative_to(self.context.workspace_policy.workspace)}{suffix}")
        return ToolResult(True, self._bounded("\n".join(entries) or "(空目录)"))


class ReadFileTool(ToolHandler):
    name = "read_file"
    description = "读取工作区内 UTF-8 文本文件。"
    parameters = ToolHandler._schema({"path": {"type": "string"}}, ["path"])

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.context.workspace_policy.resolve_path(self._required_str(arguments, "path"))
        if not path.is_file():
            return ToolResult(False, "read_file 的目标必须是文件")
        content = path.read_text(encoding="utf-8")
        return ToolResult(True, self._bounded(content))
