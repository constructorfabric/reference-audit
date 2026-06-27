"""pydantic → strict JSON schema for OpenAI structured outputs.

OpenAI `response_format=json_schema` with `strict: true` requires every object to set
`additionalProperties: false` and list ALL properties in `required`. pydantic's default schema
omits fields that have defaults from `required`, so we tighten it here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for value in node.values():
            _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)
    return node


def strict_schema(model: type[BaseModel]) -> dict:
    """Return an OpenAI strict-mode JSON schema for a pydantic model."""
    return _strictify(model.model_json_schema())


def response_format(model: type[BaseModel], name: str) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": strict_schema(model), "strict": True},
    }
