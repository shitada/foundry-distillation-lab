"""A private synthetic store for every conversation."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import uuid

from ..io import read_json, canonical

CONTRACT = Path(__file__).resolve().parent / "contract"


def validate_value(value, schema, path="$"):
    kinds = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    kind = schema.get("type")
    if kind is not None:
        if kind not in kinds:
            raise ValueError(f"Unsupported schema type at {path}: {kind}")
        if not isinstance(value, kinds[kind]) or (kind in ("integer", "number") and isinstance(value, bool)):
            raise ValueError(f"Schema type mismatch at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Value outside enum at {path}")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError(f"Missing required property at {path}.{name}")
        for name, child in value.items():
            if name in schema.get("properties", {}):
                validate_value(child, schema["properties"][name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"Unexpected property at {path}.{name}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"Too few items at {path}")
        if schema.get("uniqueItems") and len({canonical(item) for item in value}) != len(value):
            raise ValueError(f"Duplicate items at {path}")
        for index, child in enumerate(value):
            validate_value(child, schema.get("items", {}), f"{path}[{index}]")


class RetailSession:
    def __init__(self):
        spec = importlib.util.spec_from_file_location(
            f"_retail_{uuid.uuid4().hex}", Path(__file__).with_name("synthetic_store.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load bundled synthetic business module")
        self._store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._store)
        self.tools = read_json(CONTRACT / "zava_tools.json")
        self.system_prompt = (CONTRACT / "zava_system_prompt.md").read_text(encoding="utf-8")
        self._definitions = {entry["function"]["name"]: entry["function"] for entry in self.tools}

    def call(self, name, arguments):
        if name not in self._definitions:
            raise ValueError(f"Unknown retail tool: {name}")
        validate_value(arguments, self._definitions[name]["parameters"])
        return deepcopy(getattr(self._store, name)(**arguments))


__all__ = ["RetailSession", "validate_value", "CONTRACT"]
