"""JSON-schema-lite validation for tool arguments."""

from __future__ import annotations

from typing import Any, Mapping

from tools.errors import ToolArgumentInvalidError, ToolNotFoundError
from tools.models import ToolDescriptor


def _schema_from_descriptor(descriptor: ToolDescriptor) -> dict | None:
    meta = dict(descriptor.metadata or {})
    schema = meta.get("input_schema") or meta.get("json_schema")
    if isinstance(schema, Mapping):
        return dict(schema)
    ref = str(descriptor.input_schema_ref or "").strip()
    if ref and isinstance(meta.get("schemas"), Mapping):
        schemas = dict(meta["schemas"])
        candidate = schemas.get(ref)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return None


def validate_against_schema(arguments: Mapping[str, Any] | None, schema: Mapping[str, Any]) -> dict:
    """Minimal JSON Schema subset: type=object, required, properties, additionalProperties."""
    args = dict(arguments or {})
    if not isinstance(schema, Mapping):
        raise ToolArgumentInvalidError("tool_argument_invalid")

    schema_type = schema.get("type", "object")
    if schema_type != "object":
        raise ToolArgumentInvalidError("tool_argument_invalid")

    required = list(schema.get("required") or [])
    for key in required:
        if key not in args:
            raise ToolArgumentInvalidError("tool_argument_invalid")

    properties = dict(schema.get("properties") or {})
    additional = schema.get("additionalProperties", True)
    if additional is False:
        for key in args:
            if key not in properties:
                raise ToolArgumentInvalidError("tool_argument_invalid")

    for key, prop in properties.items():
        if key not in args:
            continue
        if not isinstance(prop, Mapping):
            continue
        expected = prop.get("type")
        value = args[key]
        if expected == "string" and not isinstance(value, str):
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if expected == "integer" and not isinstance(value, int):
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if expected == "number" and not isinstance(value, (int, float)):
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if expected == "boolean" and not isinstance(value, bool):
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if expected == "object" and not isinstance(value, dict):
            raise ToolArgumentInvalidError("tool_argument_invalid")
        if expected == "array" and not isinstance(value, list):
            raise ToolArgumentInvalidError("tool_argument_invalid")
        enum = prop.get("enum")
        if enum is not None and value not in list(enum):
            raise ToolArgumentInvalidError("tool_argument_invalid")
    return args


def validate_tool_args(
    arguments: Mapping[str, Any] | None,
    descriptor: ToolDescriptor,
    *,
    tool_version: str | None = None,
) -> dict:
    """Validate args against descriptor metadata schema; enforce version pin when set."""
    pin = str(tool_version or "").strip()
    if pin and pin != descriptor.version:
        raise ToolNotFoundError("tool_version_mismatch")
    schema = _schema_from_descriptor(descriptor)
    if schema is None:
        return dict(arguments or {})
    return validate_against_schema(arguments, schema)
