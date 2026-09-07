"""MCP JSON Schema 子集与运行值校验测试。"""

from __future__ import annotations

import math
import unittest

from tricoder.mcp.schema import (
    MAX_SCHEMA_BYTES,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_PROPERTIES,
    validate_json_value,
    validate_mcp_schema,
)


class MCPSchemaDefinitionTests(unittest.TestCase):
    def test_supported_recursive_subset_is_copied_and_accepted(self) -> None:
        """删除任一受支持关键字或返回原字典都会破坏远端工具契约隔离。"""
        schema: dict[str, object] = {
            "type": "object",
            "description": "查询参数",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 20,
                    "enum": ["brief", "full"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "ratio": {"type": "number", "minimum": 0, "maximum": 1.5},
                "enabled": {"type": "boolean"},
                "nothing": {"type": "null"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 3,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

        validated = validate_mcp_schema(schema)
        schema["properties"]["query"]["maxLength"] = 99  # type: ignore[index]

        self.assertEqual(20, validated["properties"]["query"]["maxLength"])  # type: ignore[index]

    def test_rejects_unsupported_schema_constructs_and_unknown_types(self) -> None:
        """引用、组合器、条件式和未知关键字不能绕开本地可执行子集。"""
        rejected = (
            {"$ref": "#/$defs/X"},
            {"type": "object", "$defs": {}},
            {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            {"type": "object", "patternProperties": {".*": {"type": "string"}}},
            {"type": "string", "if": {"type": "string"}},
            {"type": "mystery"},
            {"type": "string", "pattern": ".*"},
        )

        for schema in rejected:
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(ValueError, "不支持"):
                    validate_mcp_schema(schema)

    def test_rejects_schema_resource_limits_deterministically(self) -> None:
        """Schema 字节、深度和属性总量均必须在递归前后有确定上限。"""
        too_large = {"type": "string", "description": "x" * MAX_SCHEMA_BYTES}

        too_deep: dict[str, object] = {"type": "string"}
        for _ in range(MAX_SCHEMA_DEPTH):
            too_deep = {"type": "array", "items": too_deep}

        too_many_properties = {
            "type": "object",
            "properties": {
                f"field_{index}": {"type": "string"}
                for index in range(MAX_SCHEMA_PROPERTIES + 1)
            },
            "required": [],
            "additionalProperties": False,
        }

        cases = (
            (too_large, "字节"),
            (too_deep, "深度"),
            (too_many_properties, "属性"),
        )
        for schema, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_mcp_schema(schema)

    def test_rejects_malformed_supported_keywords(self) -> None:
        """格式错误的 required、enum、边界和 item Schema 不能被静默忽略。"""
        rejected = (
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["missing"],
                "additionalProperties": False,
            },
            {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 1},
            {"type": "string", "minLength": -1},
            {"type": "integer", "minimum": True},
            {"type": "number", "maximum": math.inf},
            {"type": "boolean", "enum": [1]},
            {"type": "string", "enum": []},
            {
                "type": "object",
                "properties": {1: {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        )

        for schema in rejected:
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    validate_mcp_schema(schema)


class MCPJSONValueTests(unittest.TestCase):
    def test_validates_all_supported_types_enum_and_boundaries(self) -> None:
        """运行校验必须执行定义阶段承诺的每一种基础约束。"""
        cases = (
            ({"type": "string", "minLength": 1, "maxLength": 3}, "ok"),
            ({"type": "integer", "minimum": 1, "maximum": 2}, 2),
            ({"type": "number", "minimum": 1, "maximum": 2.5}, 1.5),
            ({"type": "boolean", "enum": [True]}, True),
            ({"type": "null"}, None),
            (
                {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2},
                [1, 2],
            ),
        )

        for schema, value in cases:
            with self.subTest(schema=schema, value=value):
                validate_json_value(validate_mcp_schema(schema), value)

    def test_integer_and_number_reject_bool_and_non_finite_values(self) -> None:
        """Python 的 bool/int 继承关系和非有限浮点数不能扩大 JSON number 语义。"""
        cases = (
            ({"type": "integer"}, True),
            ({"type": "number"}, False),
            ({"type": "number"}, math.inf),
            ({"type": "number"}, math.nan),
        )

        for schema, value in cases:
            with self.subTest(schema=schema, value=value):
                with self.assertRaisesRegex(ValueError, "参数必须是|有限"):
                    validate_json_value(validate_mcp_schema(schema), value)

    def test_rejects_nested_unknown_properties_and_nested_invalid_values(self) -> None:
        """递归对象不能只校验顶层，否则 MCP 参数可在子对象夹带字段。"""
        schema = validate_mcp_schema(
            {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                        "additionalProperties": False,
                    }
                },
                "required": ["options"],
                "additionalProperties": False,
            }
        )

        with self.assertRaisesRegex(ValueError, "不支持额外参数：extra"):
            validate_json_value(schema, {"options": {"count": 1, "extra": "blocked"}})
        with self.assertRaisesRegex(ValueError, "options.count.*integer"):
            validate_json_value(schema, {"options": {"count": True}})

    def test_rejects_non_string_runtime_object_keys_with_stable_error(self) -> None:
        """混合类型对象键不能在额外字段排序处泄漏 Python TypeError。"""
        schema = validate_mcp_schema(
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            }
        )

        with self.assertRaisesRegex(ValueError, "对象键.*string"):
            validate_json_value(schema, {1: "bad", "other": "bad"})

    def test_rejects_values_outside_enum_or_simple_boundaries(self) -> None:
        """定义合法不代表输入合法，运行阶段必须逐项执行约束。"""
        cases = (
            ({"type": "string", "enum": ["a", "b"]}, "c"),
            ({"type": "string", "maxLength": 2}, "long"),
            ({"type": "integer", "minimum": 2}, 1),
            ({"type": "array", "items": {"type": "string"}, "maxItems": 1}, ["a", "b"]),
        )

        for raw_schema, value in cases:
            with self.subTest(schema=raw_schema, value=value):
                with self.assertRaises(ValueError):
                    validate_json_value(validate_mcp_schema(raw_schema), value)


if __name__ == "__main__":
    unittest.main()
