"""Thin language-model client interface.

`OpenAIChatLLM` wraps the official OpenAI SDK (Chat Completions with tool
calling). Tests use `ScriptedLLM`, which replays predetermined replies, so the
agent loop can be verified without network access or an API key.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from security.errors import AppError

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-native output items (Responses API: reasoning + function_call items) that must be
    # sent back verbatim on the next request. Ignored by other adapters.
    raw_items: list[dict[str, Any]] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.raw_items:
            message["_raw_items"] = self.raw_items
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return message


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply: ...


class OpenAIChatLLM:
    def __init__(self, api_key: str, model: str, base_url: str | None = None, timeout_s: float = 120) -> None:
        if not api_key:
            raise AppError("OPENAI_API_KEY is not set in .env, so the chat agent is unavailable.")
        from openai import OpenAI  # imported lazily so the rest of the app works without it

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_s, "max_retries": 2}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        try:
            extra: dict[str, Any] = {"tools": tools, "tool_choice": "auto"} if tools else {}
            clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
            response = self.client.chat.completions.create(model=self.model, messages=clean, **extra)
        except Exception as exc:  # map SDK errors by class name so this module imports without openai
            raise _friendly_openai_error(exc, self.model) from exc
        message = response.choices[0].message
        calls = [
            ToolCall(c.id, c.function.name, c.function.arguments or "{}")
            for c in (message.tool_calls or [])
            if getattr(c, "function", None) is not None
        ]
        return LLMReply(message.content, calls)


class OpenAIResponsesLLM:
    """OpenAI Responses API adapter (default). Supports function tools together with reasoning,
    which newer models do not allow on /v1/chat/completions."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None, timeout_s: float = 180,
                 reasoning_effort: str | None = None) -> None:
        if not api_key:
            raise AppError("OPENAI_API_KEY is not set in .env, so the chat agent is unavailable.")
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_s, "max_retries": 2}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model
        self.reasoning_effort = reasoning_effort

    @staticmethod
    def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for t in tools:
            fn = t.get("function", t)
            converted.append({"type": "function", "name": fn["name"], "description": fn.get("description", ""),
                              "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                              "strict": False})
        return converted

    @staticmethod
    def convert_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
        """Chat-style messages -> (instructions, Responses input items)."""
        instructions: str | None = None
        items: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                instructions = (instructions + "\n\n" if instructions else "") + str(m.get("content") or "")
            elif role == "user":
                items.append({"role": "user", "content": str(m.get("content") or "")})
            elif role == "assistant":
                if m.get("_raw_items"):
                    items.extend(m["_raw_items"])
                    continue
                if m.get("content"):
                    items.append({"role": "assistant", "content": str(m["content"])})
                for c in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": c["id"], "name": c["function"]["name"],
                                  "arguments": c["function"]["arguments"] or "{}"})
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"],
                              "output": str(m.get("content") or "")})
        return instructions, items

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        instructions, items = self.convert_messages(messages)
        params: dict[str, Any] = {"model": self.model, "input": items}
        if instructions:
            params["instructions"] = instructions
        if tools:
            params["tools"] = self.convert_tools(tools)
            params["tool_choice"] = "auto"
        if self.reasoning_effort:
            params["reasoning"] = {"effort": self.reasoning_effort}
        try:
            response = self.client.responses.create(**params)
        except Exception as exc:
            raise _friendly_openai_error(exc, self.model) from exc
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        texts: list[str] = []
        for item in response.output or []:
            kind = getattr(item, "type", None)
            if kind == "function_call":
                calls.append(ToolCall(item.call_id, item.name, item.arguments or "{}"))
            elif kind == "message":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", None) == "output_text" and getattr(part, "text", None):
                        texts.append(part.text)
            if kind in ("reasoning", "function_call", "message"):
                dumped = item.model_dump(exclude_none=True, by_alias=True) if hasattr(item, "model_dump") else dict(item)
                raw.append(dumped)
        content = "\n".join(texts) or None
        # Only replay reasoning/function-call items when the turn continues with tool results.
        return LLMReply(content, calls, raw if calls else [])


def build_llm(settings: Any) -> "LLMClient":
    """Create the configured OpenAI adapter."""
    key = settings.openai_api_key.get_secret_value()
    if settings.openai_api_style == "chat":
        return OpenAIChatLLM(key, settings.openai_model, settings.openai_base_url)
    return OpenAIResponsesLLM(key, settings.openai_model, settings.openai_base_url,
                              reasoning_effort=settings.openai_reasoning_effort or None)


_OPENAI_ERRORS = {
    "AuthenticationError": "OpenAI rejected the API key. Check OPENAI_API_KEY in .env.",
    "PermissionDeniedError": "This API key is not permitted to use the requested model or endpoint.",
    "NotFoundError": "Model '{model}' is not available to this API key. Change OPENAI_MODEL in .env.",
    "RateLimitError": "OpenAI rate limit or quota reached. Wait a moment or check your billing.",
    "APIConnectionError": "Could not reach the OpenAI API. Check your internet connection or proxy.",
    "APITimeoutError": "The OpenAI API timed out. Try again.",
    "BadRequestError": "The language model rejected the request (bad request). See the log for details.",
    "InternalServerError": "OpenAI returned a server error. Try again shortly.",
}


def _friendly_openai_error(exc: Exception, model: str) -> AppError:
    for cls in type(exc).__mro__:
        if cls.__name__ in _OPENAI_ERRORS:
            logger.warning("OpenAI error %s: %s", cls.__name__, exc)
            return AppError(_OPENAI_ERRORS[cls.__name__].format(model=model))
    logger.exception("Unexpected language-model error", exc_info=exc)
    return AppError("The language model request failed unexpectedly. See the log for details.")


class ScriptedLLM:
    """Replays a list of replies; records the messages it was given."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        self.calls.append(json.loads(json.dumps(messages, default=str)))
        if not self.replies:
            return LLMReply("No scripted reply left.")
        return self.replies.pop(0)
