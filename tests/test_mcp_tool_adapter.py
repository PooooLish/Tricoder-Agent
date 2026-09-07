"""MCP 工具名称、内部模型与输出归一化测试。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from tricoder.mcp.models import MCPCallResult, MCPToolSpec
from tricoder.mcp.tool_adapter import (
    MCPToolHandler,
    normalize_mcp_result,
    normalize_tool_name,
)
from tricoder.core.cancellation import CancellationError, CancellationToken
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.tools import ToolContext


class FakeContent:
    def __init__(self, content_type: str, **fields: object) -> None:
        self.type = content_type
        for name, value in fields.items():
            setattr(self, name, value)


class FakeResult:
    def __init__(self, content: list[object], *, is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class Unstringifiable:
    def __str__(self) -> str:
        raise AssertionError("未知 SDK 对象不得被字符串化")

    def __repr__(self) -> str:
        raise AssertionError("未知 SDK 对象不得被 repr")


class MCPToolNameTests(unittest.TestCase):
    def test_normalizes_components_to_stable_registry_name(self) -> None:
        """大小写、分隔符和非法开头必须得到确定的安全名称。"""
        self.assertEqual(
            "mcp__docs_server__read_page",
            normalize_tool_name("docs-server", "Read Page"),
        )
        self.assertEqual("mcp__x_7_docs__x", normalize_tool_name("7 docs", "工具"))

    def test_long_name_uses_stable_sha256_suffix_within_registry_limit(self) -> None:
        """超长名称必须可重现且以原始二元组哈希消除常见截断碰撞。"""
        server_id = "s"
        raw_name = "X" * 200
        expected_digest = hashlib.sha256(f"{server_id}\0{raw_name}".encode()).hexdigest()[:10]

        first = normalize_tool_name(server_id, raw_name)

        self.assertEqual(first, normalize_tool_name(server_id, raw_name))
        self.assertLessEqual(len(first), 64)
        self.assertTrue(first.endswith(f"_{expected_digest}"))


class MCPModelTests(unittest.TestCase):
    def test_tool_spec_copies_schema_and_bounds_description(self) -> None:
        """远端可变 Schema 和超长描述不能越过内部不可变模型边界。"""
        schema: dict[str, object] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        spec = MCPToolSpec(
            "docs",
            "read",
            "mcp__docs__read",
            "d" * 2_100,
            schema,
        )
        schema["properties"]["query"]["type"] = "integer"  # type: ignore[index]

        self.assertEqual(2_000, len(spec.description))
        self.assertEqual("string", spec.input_schema["properties"]["query"]["type"])  # type: ignore[index]

    def test_tool_spec_returns_fresh_nested_schema_copy_on_every_access(self) -> None:
        """调用者变异一次取得的嵌套 Schema 不能改写 spec 后续公开契约。"""
        spec = MCPToolSpec(
            "docs",
            "read",
            "mcp__docs__read",
            "读取文档",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )

        exposed = spec.input_schema
        exposed["properties"]["query"]["type"] = "integer"  # type: ignore[index]
        exposed["required"].clear()  # type: ignore[union-attr]

        subsequent = spec.input_schema
        self.assertIsNot(exposed, subsequent)
        self.assertEqual("string", subsequent["properties"]["query"]["type"])  # type: ignore[index]
        self.assertEqual(["query"], subsequent["required"])

    def test_call_result_rejects_unbounded_or_mutable_contract_fields(self) -> None:
        """内部调用结果只接受有界文本、布尔状态和不可变省略类型元组。"""
        with self.assertRaises(ValueError):
            MCPCallResult(True, "x" * 200_001)
        with self.assertRaises(TypeError):
            MCPCallResult(True, "ok", ["image"])  # type: ignore[arg-type]


class MCPResultNormalizationTests(unittest.TestCase):
    def test_real_sdk_error_result_uses_python_is_error_attribute(self) -> None:
        """真实 SDK 模型的 snake_case 错误字段必须归一化为失败。"""
        sdk_result = CallToolResult(
            content=[TextContent(type="text", text="tool failed")],
            isError=True,
        )

        normalized = normalize_mcp_result(sdk_result)

        self.assertTrue(sdk_result.is_error)
        self.assertFalse(normalized.ok)
        self.assertEqual("tool failed", normalized.text)

    def test_wire_mapping_error_alias_remains_supported(self) -> None:
        """未构造成 SDK 对象的 wire mapping 仍按 isError 识别失败。"""
        normalized = normalize_mcp_result(
            {
                "content": [{"type": "text", "text": "wire failed"}],
                "isError": True,
            }
        )

        self.assertFalse(normalized.ok)
        self.assertEqual("wire failed", normalized.text)

    def test_concatenates_only_text_and_emits_payload_free_placeholders(self) -> None:
        """图片、音频、资源与 blob 的 payload 哨兵绝不能进入模型文本。"""
        payloads = {
            "image": "BASE64-IMAGE-SENTINEL",
            "audio": "BASE64-AUDIO-SENTINEL",
            "resource": "PRIVATE-RESOURCE-TEXT-SENTINEL",
            "blob": "PRIVATE-BLOB-SENTINEL",
        }
        content: list[object] = [FakeContent("text", text="first")]
        content.extend(
            FakeContent(kind, data=payload, blob=payload, text=payload)
            for kind, payload in payloads.items()
        )
        content.append(
            FakeContent("resource_link", uri="PRIVATE-RESOURCE-LINK-SENTINEL")
        )
        content.append(FakeContent("text", text="second"))

        normalized = normalize_mcp_result(FakeResult(content))

        self.assertTrue(normalized.ok)
        self.assertIn("first", normalized.text)
        self.assertIn("second", normalized.text)
        for kind, payload in payloads.items():
            self.assertIn(f"[已省略 MCP {kind} 内容]", normalized.text)
            self.assertNotIn(payload, normalized.text)
        self.assertNotIn("PRIVATE-RESOURCE-LINK-SENTINEL", normalized.text)
        self.assertEqual(2, normalized.text.count("[已省略 MCP resource 内容]"))
        self.assertEqual(("image", "audio", "resource", "blob"), normalized.omitted_content_types)

    def test_result_error_flag_and_total_character_ceiling_are_preserved(self) -> None:
        """isError 必须映射失败，拼接后正文不得超过调用方或传输硬上限。"""
        short_limit = normalize_mcp_result(
            FakeResult([FakeContent("text", text="x" * 100)], is_error=True),
            max_chars=20,
        )
        hard_limit = normalize_mcp_result(
            FakeResult([FakeContent("text", text="x" * 200_100)]),
            max_chars=500_000,
        )

        self.assertFalse(short_limit.ok)
        self.assertLessEqual(len(short_limit.text), 20)
        self.assertLessEqual(len(hard_limit.text), 200_000)

    def test_unknown_objects_are_omitted_without_stringification(self) -> None:
        """未知 SDK 内容及未知 text 字段对象不能经 str/repr 泄露。"""
        unknown = Unstringifiable()
        normalized = normalize_mcp_result(
            FakeResult(
                [
                    unknown,
                    FakeContent("text", text=unknown),
                    FakeContent("future", payload=unknown),
                ]
            )
        )

        self.assertNotIn("Unstringifiable", normalized.text)
        self.assertIn("unknown", normalized.omitted_content_types)


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], CancellationToken]] = []

    async def call_tool(
        self,
        server_id: str,
        raw_name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult:
        self.calls.append((server_id, raw_name, arguments, cancellation))
        return MCPCallResult(True, "safe text", ("image",))


class MCPToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        workspace = Path(self.temp.name).resolve()
        self.context = ToolContext(
            WorkspacePolicy(workspace),
            CommandPolicy(workspace),
            approver=lambda _action, _detail: True,
        )
        self.spec = MCPToolSpec(
            "docs",
            "Read Page",
            "mcp__docs__read_page",
            "读取页面",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        self.manager = _FakeManager()
        self.handler = MCPToolHandler(self.context, self.manager, self.spec)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_sync_fails_and_async_maps_internal_result_without_new_loop(self) -> None:
        """MCP handler 误走同步入口时不能创建事件循环，异步入口必须保留结果状态。"""
        token = CancellationToken()

        sync_result = self.handler.run({"query": "x"})
        async_result = await self.handler.run_async(
            {"query": "x"},
            cancellation=token,
        )

        self.assertEqual("dangerous", self.handler.risk)
        self.assertFalse(sync_result.ok)
        self.assertEqual("MCP 工具仅支持异步执行", sync_result.output)
        self.assertTrue(async_result.ok)
        self.assertEqual("safe text", async_result.output)
        self.assertEqual(
            [("docs", "Read Page", {"query": "x"}, token)],
            self.manager.calls,
        )

    async def test_pre_cancelled_call_propagates_without_reaching_manager(self) -> None:
        """已取消工具调用必须在 manager 和 server 前终止，且不能伪装为普通失败。"""
        token = CancellationToken()
        token.cancel()

        with self.assertRaises(CancellationError):
            await self.handler.run_async({"query": "x"}, cancellation=token)

        self.assertEqual([], self.manager.calls)


if __name__ == "__main__":
    unittest.main()
