"""Small, fail-closed JSON Schema subset for the retail function contracts."""

import math
import re

from ..io import canonical, parse_json as loads


class ContractError(ValueError):
    """Invalid evaluation input, not a model quality result."""


def equal(left, right):
    # JSON boolean true must not compare equal to integer 1.
    return canonical(left) == canonical(right)


_ANNOTATIONS = {"description", "title", "$schema", "default", "examples"}
_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items",
             "minItems", "maxItems", "uniqueItems", "enum", "const", "minimum",
             "maximum", "minLength", "maxLength", "pattern"}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def check_schema(schema):
    if not isinstance(schema, dict) or set(schema) - _ANNOTATIONS - _KEYWORDS:
        raise ContractError("unsupported_or_malformed_schema")
    if not isinstance(schema.get("type"), str) or schema["type"] not in _TYPES:
        raise ContractError("schema_requires_one_supported_type")
    kind = schema["type"]
    properties = schema.get("properties", {})
    if kind == "object":
        if not isinstance(properties, dict) or any(not isinstance(k, str) for k in properties):
            raise ContractError("invalid_schema_properties")
        required = schema.get("required", [])
        if (not isinstance(required, list) or any(not isinstance(k, str) for k in required)
                or len(set(required)) != len(required) or set(required) - properties.keys()):
            raise ContractError("invalid_schema_required")
        if schema.get("additionalProperties", False) is not False:
            raise ContractError("retail_contract_requires_closed_objects")
        for child in properties.values():
            check_schema(child)
    elif any(key in schema for key in ("properties", "required", "additionalProperties")):
        raise ContractError("object_keywords_on_nonobject")
    if kind == "array":
        check_schema(schema.get("items"))
    elif any(key in schema for key in ("items", "minItems", "maxItems", "uniqueItems")):
        raise ContractError("array_keywords_on_nonarray")
    if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
        raise ContractError("invalid_uniqueItems")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ContractError(f"invalid_{key}")
    if any(key in schema for key in ("minLength", "maxLength", "pattern")) and kind != "string":
        raise ContractError("string_keywords_on_nonstring")
    for key in ("minimum", "maximum"):
        if key in schema and (kind not in {"integer", "number"}
                              or type(schema[key]) not in (int, float)
                              or not math.isfinite(schema[key])):
            raise ContractError(f"invalid_{key}")
    for low, high in (("minItems", "maxItems"), ("minLength", "maxLength"), ("minimum", "maximum")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ContractError("inverted_schema_bounds")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (TypeError, re.error) as exc:
            raise ContractError("invalid_schema_pattern") from exc
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ContractError("invalid_schema_enum")
    try:
        canonical(schema)
    except (ValueError, TypeError) as exc:
        raise ContractError("schema_not_json") from exc


def validate(value, schema, path="$"):
    """Return validation errors; schema must already have passed check_schema."""
    kind = schema["type"]
    valid_type = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) is int or (type(value) is float and math.isfinite(value)),
        "boolean": type(value) is bool, "null": value is None,
    }[kind]
    if not valid_type:
        return [f"{path}:expected_{kind}"]
    errors = []
    if "enum" in schema and not any(equal(value, candidate) for candidate in schema["enum"]):
        errors.append(f"{path}:enum")
    if "const" in schema and not equal(value, schema["const"]):
        errors.append(f"{path}:const")
    if kind == "object":
        properties = schema.get("properties", {})
        errors.extend(f"{path}.{key}:required" for key in schema.get("required", []) if key not in value)
        for key, child in value.items():
            if key not in properties:
                errors.append(f"{path}.{key}:unknown_property")
            else:
                errors.extend(validate(child, properties[key], f"{path}.{key}"))
    if kind == "array":
        for index, child in enumerate(value):
            errors.extend(validate(child, schema["items"], f"{path}[{index}]"))
        if schema.get("uniqueItems") and len({canonical(v) for v in value}) != len(value):
            errors.append(f"{path}:duplicate_items")
    for low, high, observed in (
        ("minItems", "maxItems", len(value) if kind == "array" else None),
        ("minLength", "maxLength", len(value) if kind == "string" else None),
        ("minimum", "maximum", value if kind in {"integer", "number"} else None),
    ):
        if observed is not None:
            if low in schema and observed < schema[low]:
                errors.append(f"{path}:{low}")
            if high in schema and observed > schema[high]:
                errors.append(f"{path}:{high}")
    if kind == "string" and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}:pattern")
    return errors


def tool_schemas(tools):
    if not isinstance(tools, list) or not tools:
        raise ContractError("nonempty_tools_required")
    schemas = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ContractError("invalid_tool_schema")
        function = tool.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
            raise ContractError("invalid_tool_name")
        name = function["name"]
        if name in schemas:
            raise ContractError("duplicate_tool_name")
        check_schema(function.get("parameters"))
        if function["parameters"]["type"] != "object":
            raise ContractError("tool_arguments_must_be_object")
        schemas[name] = function["parameters"]
    return schemas


def parse_call(call):
    if not isinstance(call, dict):
        raise ValueError("call_not_object")
    function = call.get("function", call)
    if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
        raise ValueError("invalid_call_name")
    if "type" in call and call["type"] != "function":
        raise ValueError("unsupported_call_type")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        arguments = loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("arguments_not_object")
    canonical(arguments)
    return {"name": function["name"], "arguments": arguments}


def call_errors(call, schemas):
    if call["name"] not in schemas:
        return ["unknown_tool"]
    return validate(call["arguments"], schemas[call["name"]])
