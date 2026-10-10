"""Agent loop with a scripted model: tool use, grounding, rejection handling, history."""

from __future__ import annotations

import json

from agents.grounding import unverified_numbers
from agents.llm import LLMReply, ScriptedLLM, ToolCall
from agents.orchestrator import DataAgent
from analysis.csv_loader import load_csv
from tests.conftest import requires_db
from tools import registry
from tools.context import CSVSource


def call(tool_name: str, **args) -> LLMReply:
    return LLMReply(None, [ToolCall(f"call_{tool_name}", tool_name, json.dumps(args))])


def test_all_required_tools_registered():
    expected = {
        "list_available_sources", "list_database_schemas", "list_database_tables", "describe_table",
        "preview_table", "validate_sql", "execute_read_only_sql", "inspect_csv", "profile_data_quality",
        "join_data_sources", "run_statistical_analysis", "generate_visualisation", "generate_geospatial_map",
        "run_forecast", "detect_anomalies", "save_dashboard", "export_reviewed_results",
        "check_spatial_data",  # added in Phase 3
    }
    assert expected == set(registry.names)
    for schema in registry.schemas():
        assert schema["function"]["parameters"]["type"] == "object"


def test_agent_csv_flow_and_history(offline_ctx, sample_csv_bytes):
    loaded = load_csv("sales.csv", sample_csv_bytes)
    offline_ctx.csv_sources["sales.csv"] = CSVSource("sales.csv", loaded.raw, loaded.data, loaded.inferred_types)
    llm = ScriptedLLM([
        call("inspect_csv", name="sales.csv"),
        call("run_statistical_analysis", source="csv:sales.csv", method="group_summary",
             params={"group": "branch", "value": "revenue_gbp"}),
        LLMReply("**Direct answer**: Northern Quarter has the highest revenue."),
    ])
    agent = DataAgent(llm, offline_ctx)
    turn = agent.ask("Which branch has the highest revenue?")
    assert turn.status == "success", turn.answer
    assert [e.tool for e in turn.events] == ["inspect_csv", "run_statistical_analysis"]
    assert turn.result_ids and not turn.unverified
    # The model received tool outputs, never raw full data.
    tool_msgs = [m for m in llm.calls[-1] if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and all(len(m["content"]) < 15_000 for m in tool_msgs)
    history = offline_ctx.store.list_history()
    assert history[0].question.startswith("Which branch") and "run_statistical_analysis" in history[0].methods


def test_follow_up_keeps_context(offline_ctx):
    llm = ScriptedLLM([LLMReply("First answer."), LLMReply("Second answer.")])
    agent = DataAgent(llm, offline_ctx)
    agent.ask("first question")
    agent.ask("and the follow-up?")
    roles = [m["role"] for m in llm.calls[-1]]
    contents = " ".join(str(m.get("content")) for m in llm.calls[-1])
    assert roles[0] == "system" and "first question" in contents and "First answer." in contents


def test_fabricated_numbers_flagged(offline_ctx):
    llm = ScriptedLLM([LLMReply("Total revenue was £1,234,567.89 across 4,321 transactions.")])
    turn = DataAgent(llm, offline_ctx).ask("total revenue?")
    assert "£1,234,567.89" in turn.unverified[0] and any("4,321" in u for u in turn.unverified)
    assert "Numbers not found" in " ".join(offline_ctx.store.list_history()[0].warnings)


def test_grounding_accepts_rounded_and_percent_values():
    outputs = [json.dumps({"total": 1234567.891, "share": 0.4567, "n": 5321, "growth_pct": 12.345})]
    assert unverified_numbers("Revenue £1,234,567.89 (£1.2m), share 45.7%, n = 5,321, growth 12.3%", outputs) == []
    assert unverified_numbers("Revenue grew 18.9% in 2025 over 3 months", outputs) == ["18.9%"]


def test_step_limit(offline_ctx):
    llm = ScriptedLLM([call("list_available_sources")] * 5)
    turn = DataAgent(llm, offline_ctx, max_steps=3).ask("loop forever")
    assert turn.status == "partial" and len(turn.events) == 3


def test_unknown_tool_and_bad_arguments_are_safe(offline_ctx):
    llm = ScriptedLLM([
        LLMReply(None, [ToolCall("c1", "drop_database", "{}"), ToolCall("c2", "inspect_csv", "{not json")]),
        LLMReply("I could not do that."),
    ])
    turn = DataAgent(llm, offline_ctx).ask("do something bad")
    assert [e.ok for e in turn.events] == [False, False]
    assert turn.answer == "I could not do that."


def test_database_unavailable_message(offline_ctx):
    offline_ctx.selected_sources = ["database"]
    out = registry.dispatch(offline_ctx, "execute_read_only_sql", {"sql": "SELECT 1"})
    assert not out.ok and "not connected" in out.payload["error"]


@requires_db
def test_agent_destructive_sql_rejected_then_corrected(live_ctx, demo_sales):
    llm = ScriptedLLM([
        call("execute_read_only_sql", sql="DELETE FROM training.demo_sales", purpose="oops"),
        call("execute_read_only_sql", sql="SELECT count(*) AS n FROM training.demo_sales", purpose="count rows"),
        LLMReply("There are rows in the table."),
    ])
    turn = DataAgent(llm, live_ctx).ask("how many sales rows?")
    assert [e.ok for e in turn.events] == [False, True]
    assert "Only SELECT" in turn.events[0].summary
    assert len(turn.sql) == 1 and turn.sql[0].startswith("SELECT COUNT(*)")
    audit = live_ctx.store.recent_audit(5)
    assert {"rejected", "executed"} <= {a["verdict"] for a in audit}


@requires_db
def test_system_prompt_has_no_secrets(live_ctx):
    from agents.prompts import build_system_prompt

    prompt = build_system_prompt(live_ctx)
    secret = live_ctx.settings.pg_password.get_secret_value()
    assert secret not in prompt and live_ctx.settings.pg_host not in prompt
    assert "training" in prompt


def test_openai_adapter_parses_tool_calls_and_maps_errors():
    from types import SimpleNamespace

    import pytest

    from agents.llm import OpenAIChatLLM
    from security.errors import AppError

    def fake_create(**kwargs):
        assert kwargs["model"] == "gpt-5.6-luna" and kwargs["tool_choice"] == "auto"
        fn = SimpleNamespace(name="list_available_sources", arguments="{}")
        msg = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="c1", function=fn)])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    llm = OpenAIChatLLM.__new__(OpenAIChatLLM)
    llm.model = "gpt-5.6-luna"
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    reply = llm.complete([{"role": "user", "content": "hi"}], registry.schemas())
    assert reply.tool_calls[0].name == "list_available_sources"
    assert reply.as_message()["tool_calls"][0]["function"]["arguments"] == "{}"

    class NotFoundError(Exception):
        pass

    def failing_create(**kwargs):
        raise NotFoundError("model missing")

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=failing_create)))
    with pytest.raises(AppError, match="gpt-5.6-luna' is not available"):
        llm.complete([], [])


def test_responses_adapter_round_trip():
    """Responses API: tools converted, reasoning/function_call items replayed, outputs mapped."""
    from types import SimpleNamespace

    from agents.llm import OpenAIResponsesLLM

    class Item(SimpleNamespace):
        def model_dump(self, **kwargs):
            return {k: v for k, v in self.__dict__.items() if v is not None}

    seen = []

    def fake_create(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return SimpleNamespace(output=[
                Item(type="reasoning", id="rs_1", summary=[]),
                Item(type="function_call", id="fc_1", call_id="call_1", name="list_available_sources", arguments="{}"),
            ])
        text = SimpleNamespace(type="output_text", text="Here are your sources.")
        return SimpleNamespace(output=[Item(type="message", id="msg_1", role="assistant", content=[text])])

    llm = OpenAIResponsesLLM.__new__(OpenAIResponsesLLM)
    llm.model, llm.reasoning_effort = "gpt-5.6-luna", "medium"
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))

    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "what sources?"}]
    first = llm.complete(msgs, registry.schemas())
    assert first.tool_calls[0].name == "list_available_sources"
    assert seen[0]["instructions"] == "SYS" and seen[0]["reasoning"] == {"effort": "medium"}
    assert seen[0]["tools"][0]["type"] == "function" and "name" in seen[0]["tools"][0]
    assert "function" not in seen[0]["tools"][0]

    msgs += [first.as_message(), {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}]
    second = llm.complete(msgs, registry.schemas())
    assert second.content == "Here are your sources." and not second.tool_calls
    replay = seen[1]["input"]
    assert [i.get("type", i.get("role")) for i in replay] == ["user", "reasoning", "function_call", "function_call_output"]
    assert replay[-1]["call_id"] == "call_1"


def test_chat_adapter_strips_private_keys():
    from types import SimpleNamespace

    from agents.llm import OpenAIChatLLM

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))])

    llm = OpenAIChatLLM.__new__(OpenAIChatLLM)
    llm.model = "m"
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    llm.complete([{"role": "assistant", "content": "", "_raw_items": [{"type": "reasoning"}]}], [])
    assert "_raw_items" not in captured["messages"][0]
