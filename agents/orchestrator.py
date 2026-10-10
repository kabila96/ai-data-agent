"""The agent loop: model ↔ tools, with a step limit and grounding check.

Flow per user turn:
  system prompt + compacted conversation → model → (tool calls → registry → results)* → final answer
The model only sees tool outputs produced by `ToolOutput.to_model_json`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agents.grounding import unverified_numbers
from agents.llm import LLMClient
from agents.prompts import build_system_prompt
from models.schemas import HistoryEntry, ToolEvent
from security.errors import AppError, to_user_message
from tools import registry
from tools.context import AgentContext

logger = logging.getLogger(__name__)

OLD_TOOL_OUTPUT_CHARS = 1_500
MAX_HISTORY_MESSAGES = 40


@dataclass
class AgentTurn:
    question: str
    answer: str
    events: list[ToolEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    status: str = "success"

    @property
    def sql(self) -> list[str]:
        return [e.sql for e in self.events if e.sql and e.tool == "execute_read_only_sql" and e.ok]

    @property
    def result_ids(self) -> list[str]:
        return [e.result_id for e in self.events if e.result_id]

    @property
    def chart_ids(self) -> list[str]:
        return [e.chart_id for e in self.events if e.chart_id]

    @property
    def needs_approval(self) -> bool:
        return any(e.requires_approval for e in self.events)


class DataAgent:
    def __init__(self, llm: LLMClient, ctx: AgentContext, max_steps: int | None = None) -> None:
        self.llm = llm
        self.ctx = ctx
        self.max_steps = max_steps or ctx.settings.agent_max_steps
        self.messages: list[dict[str, Any]] = []  # conversation without the system prompt

    def reset(self) -> None:
        self.messages.clear()

    def _compacted_history(self) -> list[dict[str, Any]]:
        """Shorten tool outputs from earlier turns to keep follow-ups cheap."""
        history = self.messages[-MAX_HISTORY_MESSAGES:]
        # Never start with an orphaned tool message.
        while history and history[0]["role"] == "tool":
            history = history[1:]
        while history and history[0]["role"] == "assistant" and history[0].get("tool_calls"):
            history = history[1:]
            while history and history[0]["role"] == "tool":
                history = history[1:]
        compacted = []
        for msg in history:
            if msg["role"] == "tool" and len(msg["content"]) > OLD_TOOL_OUTPUT_CHARS:
                msg = {**msg, "content": msg["content"][:OLD_TOOL_OUTPUT_CHARS] + '…"(truncated earlier output)"'}
            compacted.append(msg)
        return compacted

    def ask(self, question: str) -> AgentTurn:
        turn = AgentTurn(question=question, answer="")
        question = (question or "").strip()[:8000]
        if not question:
            turn.answer, turn.status = "Please type a question.", "clarification"
            return turn
        prior = self._compacted_history()
        new_messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        tool_outputs: list[str] = []
        tools = registry.schemas()
        try:
            for _ in range(self.max_steps):
                messages = [{"role": "system", "content": build_system_prompt(self.ctx)}, *prior, *new_messages]
                reply = self.llm.complete(messages, tools)
                new_messages.append(reply.as_message())
                if not reply.tool_calls:
                    turn.answer = (reply.content or "").strip() or "(The model returned an empty answer.)"
                    break
                for call in reply.tool_calls:
                    output = registry.dispatch(self.ctx, call.name, call.arguments)
                    content = output.to_model_json()
                    tool_outputs.append(content)
                    new_messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                    try:
                        arguments = json.loads(call.arguments) if call.arguments else {}
                    except json.JSONDecodeError:
                        arguments = {"raw": call.arguments[:500]}
                    turn.events.append(ToolEvent(
                        tool=call.name, arguments=arguments, ok=output.ok,
                        summary=str(output.payload.get("error") or output.payload.get("status") or output.method or ""),
                        result_id=output.result_id, chart_id=output.chart_id, sql=output.sql,
                        warnings=output.warnings, requires_approval=output.requires_approval,
                    ))
                    turn.warnings.extend(w for w in output.warnings if w not in turn.warnings)
            else:
                turn.answer = (
                    f"I stopped after {self.max_steps} analysis steps without reaching a final answer. "
                    "The partial results are shown below; try narrowing the question."
                )
                turn.status = "partial"
        except AppError as exc:
            turn.answer, turn.status = exc.user_message, "error"
        except Exception as exc:
            turn.answer, turn.status = to_user_message(exc, "agent request"), "error"

        if turn.status == "success":
            turn.unverified = unverified_numbers(turn.answer, tool_outputs, question)
            if turn.needs_approval:
                turn.status = "clarification"
        # Persist the conversation (only completed tool-call groups are kept).
        if turn.status != "error":
            self.messages.extend(new_messages)
        self._record_history(turn)
        return turn

    def _record_history(self, turn: AgentTurn) -> None:
        sources = sorted({
            r.source_label for rid in turn.result_ids if (r := self.ctx.results.get(rid)) is not None
        }) or list(self.ctx.selected_sources)
        methods = []
        for e in turn.events:
            label = e.tool if e.ok else f"{e.tool} (failed)"
            if label not in methods:
                methods.append(label)
        charts = [self.ctx.charts[c].spec.title for c in turn.chart_ids if c in self.ctx.charts]
        errors = [e.summary for e in turn.events if not e.ok and e.summary]
        if turn.status == "error":
            errors.append(turn.answer)
        warnings = list(turn.warnings)
        if turn.unverified:
            warnings.append("Numbers not found in tool outputs: " + ", ".join(turn.unverified[:10]))
        try:
            self.ctx.store.add_history(HistoryEntry(
                question=turn.question, sources=sources, sql=turn.sql,
                status=turn.status if turn.status in ("success", "partial", "error", "clarification") else "success",
                methods=methods, charts=charts, warnings=warnings[:30], errors=errors[:20],
            ))
        except Exception:  # history must never break the chat
            logger.exception("Failed to write history")
