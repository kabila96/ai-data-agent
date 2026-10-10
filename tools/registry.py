"""Tool registry: typed arguments, JSON schemas for the model, safe dispatch.

The model can only call tools registered here. Arguments are validated with
Pydantic before any handler runs, and every handler's exceptions are converted
into safe messages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from security.errors import to_user_message
from tools.context import AgentContext

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 14_000


@dataclass
class ToolOutput:
    ok: bool
    payload: dict[str, Any]
    result_id: str | None = None
    chart_id: str | None = None
    sql: str | None = None
    warnings: list[str] = field(default_factory=list)
    requires_approval: bool = False
    method: str | None = None

    def to_model_json(self) -> str:
        body = dict(self.payload)
        body["ok"] = self.ok
        if self.result_id:
            body["result_id"] = self.result_id
        if self.chart_id:
            body["chart_id"] = self.chart_id
        if self.warnings:
            body["warnings"] = self.warnings[:20]
        if self.requires_approval:
            body["requires_user_approval"] = True
        text = json.dumps(body, default=str, ensure_ascii=False)
        if len(text) > MAX_TOOL_OUTPUT_CHARS:
            body.pop("sample_rows", None)
            body["note"] = "Output truncated; sample rows removed to stay within limits."
            text = json.dumps(body, default=str, ensure_ascii=False)[:MAX_TOOL_OUTPUT_CHARS]
        return text


Handler = Callable[[AgentContext, Any], ToolOutput]


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler

    def openai_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, description: str, args_model: type[BaseModel]) -> Callable[[Handler], Handler]:
        def decorator(func: Handler) -> Handler:
            if name in self._tools:
                raise ValueError(f"Tool {name} already registered")
            self._tools[name] = ToolSpec(name, description, args_model, func)
            return func
        return decorator

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]

    def dispatch(self, ctx: AgentContext, name: str, raw_arguments: str | dict[str, Any] | None) -> ToolOutput:
        spec = self._tools.get(name)
        if spec is None:
            return ToolOutput(False, {"error": f"Unknown tool '{name}'. Available: {', '.join(self.names)}"})
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments.strip() else (raw_arguments or {})
            args = spec.args_model.model_validate(arguments)
        except (json.JSONDecodeError, ValidationError) as exc:
            detail = exc.errors()[:5] if isinstance(exc, ValidationError) else str(exc)
            return ToolOutput(False, {"error": "Invalid arguments", "detail": json.loads(json.dumps(detail, default=str))})
        try:
            return spec.handler(ctx, args)
        except Exception as exc:  # converted to a safe message, logged in full
            return ToolOutput(False, {"error": to_user_message(exc, f"tool {name}")})


registry = ToolRegistry()
