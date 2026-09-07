"""TriCoder 可执行的有界 JSON Schema 子集。"""

from __future__ import annotations

import json
import math
from typing import Any


MAX_SCHEMA_BYTES = 32_768
MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_PROPERTIES = 128

_COMMON_KEYS = {"type", "description", "enum"}
_TYPE_KEYS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}


def validate_mcp_schema(schema: object) -> dict[str, object]:
    """验证并复制 MCP Schema，拒绝不可执行或资源无界的定义。"""

    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("MCP Schema 必须是有限的 JSON 对象") from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError(f"MCP Schema 字节数超过上限 {MAX_SCHEMA_BYTES}")
    _reject_non_string_object_keys(schema)

    # JSON 往返同时移除 dict/list 子类，避免把可变 SDK 对象保存在内部契约中。
    copied = json.loads(encoded)
    if not isinstance(copied, dict):
        raise ValueError("MCP Schema 必须是对象")

    property_count = [0]
    _validate_schema_node(copied, depth=1, property_count=property_count)
    return copied


def _reject_non_string_object_keys(value: object) -> None:
    """JSON 编码器会把整数键改写为字符串，因此需在复制前显式拒绝。"""

    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("MCP Schema 属性名必须是字符串")
        for child in value.values():
            _reject_non_string_object_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_non_string_object_keys(child)


def _validate_schema_node(
    schema: dict[str, Any],
    *,
    depth: int,
    property_count: list[int],
) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise ValueError(f"MCP Schema 深度超过上限 {MAX_SCHEMA_DEPTH}")
    if not all(isinstance(key, str) for key in schema):
        raise ValueError("MCP Schema 属性名必须是字符串")

    expected = schema.get("type")
    allowed_for_type = _TYPE_KEYS.get(expected) if isinstance(expected, str) else None
    if allowed_for_type is None:
        unsupported = sorted(set(schema) - _COMMON_KEYS)
        if unsupported:
            raise ValueError(f"MCP Schema 不支持关键字：{unsupported[0]}")
        if expected is None:
            raise ValueError("MCP Schema 必须声明 type")
        raise ValueError(f"MCP Schema 不支持类型：{expected}")

    unsupported = sorted(set(schema) - _COMMON_KEYS - allowed_for_type)
    if unsupported:
        raise ValueError(f"MCP Schema 不支持关键字：{unsupported[0]}")

    description = schema.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("MCP Schema description 必须是字符串")

    if expected == "object":
        _validate_object_schema(schema, depth=depth, property_count=property_count)
    elif expected == "array":
        _validate_array_schema(schema, depth=depth, property_count=property_count)
    elif expected == "string":
        _validate_non_negative_bounds(schema, "minLength", "maxLength")
    elif expected in {"integer", "number"}:
        _validate_numeric_bounds(schema)

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise ValueError("MCP Schema enum 必须是非空标量列表")
        for item in enum:
            if not _is_scalar(item):
                raise ValueError("MCP Schema enum 只支持标量")
            _validate_json_value(schema, item, path="enum")


def _validate_object_schema(
    schema: dict[str, Any],
    *,
    depth: int,
    property_count: list[int],
) -> None:
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("MCP object Schema 缺少 properties 或 required")
    if schema.get("additionalProperties") is not False:
        raise ValueError("MCP object Schema 的 additionalProperties 必须是 false")
    if not all(isinstance(name, str) and name for name in properties):
        raise ValueError("MCP object Schema 的属性名必须是非空字符串")
    if not all(isinstance(name, str) and name in properties for name in required):
        raise ValueError("MCP object Schema 的 required 无效")
    if len(set(required)) != len(required):
        raise ValueError("MCP object Schema 的 required 不能重复")

    property_count[0] += len(properties)
    if property_count[0] > MAX_SCHEMA_PROPERTIES:
        raise ValueError(f"MCP Schema 属性总数超过上限 {MAX_SCHEMA_PROPERTIES}")
    for name in sorted(properties):
        child = properties[name]
        if not isinstance(child, dict):
            raise ValueError(f"MCP Schema 属性 {name} 必须是 Schema 对象")
        _validate_schema_node(child, depth=depth + 1, property_count=property_count)


def _validate_array_schema(
    schema: dict[str, Any],
    *,
    depth: int,
    property_count: list[int],
) -> None:
    items = schema.get("items")
    if not isinstance(items, dict):
        raise ValueError("MCP array Schema 缺少 items")
    _validate_non_negative_bounds(schema, "minItems", "maxItems")
    _validate_schema_node(items, depth=depth + 1, property_count=property_count)


def _validate_non_negative_bounds(
    schema: dict[str, Any],
    minimum_name: str,
    maximum_name: str,
) -> None:
    minimum = schema.get(minimum_name)
    maximum = schema.get(maximum_name)
    for name, value in ((minimum_name, minimum), (maximum_name, maximum)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"MCP Schema {name} 必须是非负整数")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"MCP Schema {minimum_name} 不能大于 {maximum_name}")


def _validate_numeric_bounds(schema: dict[str, Any]) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    for name, value in (("minimum", minimum), ("maximum", maximum)):
        if value is not None and not _is_finite_number(value):
            raise ValueError(f"MCP Schema {name} 必须是有限数字")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("MCP Schema minimum 不能大于 maximum")


def validate_json_value(schema: dict[str, object], value: object) -> None:
    """按已验证的 Schema 子集递归校验一个 JSON 值。"""

    if not isinstance(schema, dict):
        raise ValueError("MCP Schema 必须是对象")
    _validate_json_value(schema, value, path="参数")


def _validate_json_value(
    schema: dict[str, Any],
    value: object,
    *,
    path: str,
) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path}必须是 object")
        if not all(isinstance(name, str) for name in value):
            raise ValueError(f"{path}对象键必须是 string")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise ValueError(f"缺少必填参数：{name}")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise ValueError(f"不支持额外参数：{sorted(extras)[0]}")
        for name, child_value in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                _validate_json_value(
                    child_schema,
                    child_value,
                    path=f"参数 {name}" if path == "参数" else f"{path}.{name}",
                )
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path}必须是 array")
        _validate_length(value, schema, "minItems", "maxItems", path)
        item_schema = schema["items"]
        for index, item in enumerate(value):
            _validate_json_value(item_schema, item, path=f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path}必须是 string")
        _validate_length(value, schema, "minLength", "maxLength", path)
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{path}必须是 integer")
        _validate_number(value, schema, path)
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{path}必须是 number")
        if not _is_finite_number(value):
            raise ValueError(f"{path}必须是有限 number")
        _validate_number(value, schema, path)
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path}必须是 boolean")
    elif expected == "null":
        if value is not None:
            raise ValueError(f"{path}必须是 null")
    else:
        raise ValueError(f"MCP Schema 不支持类型：{expected}")

    enum = schema.get("enum")
    if enum is not None and not any(_scalar_equal(value, candidate) for candidate in enum):
        raise ValueError(f"{path}不在允许的 enum 中")


def _validate_length(
    value: str | list[object],
    schema: dict[str, Any],
    minimum_name: str,
    maximum_name: str,
    path: str,
) -> None:
    minimum = schema.get(minimum_name)
    maximum = schema.get(maximum_name)
    if minimum is not None and len(value) < minimum:
        raise ValueError(f"{path}长度小于 {minimum_name}")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{path}长度超过 {maximum_name}")


def _validate_number(value: int | float, schema: dict[str, Any], path: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path}小于 minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{path}大于 maximum")


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _scalar_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right
