"""只供本仓库 MCP stdio 集成测试使用的确定性本地 server。"""

from __future__ import annotations

import sys
import time

# mcp==2.1.1 将 FastMCP 重命名为 MCPServer。
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool


_DELAY_RESPONSE = "--delay" in sys.argv[1:]
_EXIT_IMMEDIATELY = "--exit-immediately" in sys.argv[1:]


def _delay_if_requested() -> None:
    if _DELAY_RESPONSE:
        time.sleep(1.0)


def echo(text: str) -> str:
    """返回确定性测试文本。"""

    _delay_if_requested()
    return f"echo:{text}"


def bounded_large_output(size: int) -> str:
    """生成可预测的大结果，用于验证客户端输出边界。"""

    _delay_if_requested()
    return "X" * min(max(size, 0), 200_000)


def _strict_tool(function, schema: dict[str, object]) -> Tool:
    """让 fixture 发布 TriCoder 可执行的严格 Schema 子集。"""

    tool = Tool.from_function(function)
    tool.parameters = schema
    return tool


mcp = MCPServer(
    "tricoder-test",
    tools=[
        _strict_tool(
            echo,
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
        _strict_tool(
            bounded_large_output,
            {
                "type": "object",
                "properties": {"size": {"type": "integer"}},
                "required": ["size"],
                "additionalProperties": False,
            },
        ),
    ],
)


if __name__ == "__main__":
    if not _EXIT_IMMEDIATELY:
        mcp.run(transport="stdio")
