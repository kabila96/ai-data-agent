# Architecture

## Layers

```text
Streamlit pages  ─────────────────────────────────────────────┐
  chat · explorer · csv · visualise · dashboards · history    │ session state (AgentContext)
                                                              │   csv_sources, results, charts,
Agent orchestrator (agents/orchestrator.py)                   │   approvals, selected sources
  system prompt + compacted history → LLM → tool calls → …    │
  step limit · grounding check · history record               │
                                                              │
Tool registry (tools/)  ── Pydantic-validated arguments ──────┘
  17 tools; each returns a compact JSON summary for the model
  and keeps full DataFrames in-process
        │               │                │                 │
   security/        analysis/       visualisations/    dashboards/
   sql_validator    profiling       chart_selector     save / refresh /
   allowlists       joins, stats    charts, theme      filters / export
   redaction        transforms
        │
   database/  engine (read-only role) · metadata (fixed catalog SQL) · executor (RO txn, timeout, cap, audit)
        │
   PostgreSQL / PostGIS: powell_analytics            storage/: SQLite (history, audit, dictionary, dashboards)
```

## Request flow (chat)

1. The user asks a question. `DataAgent.ask` builds the system prompt. The prompt contains the selected sources and the analysis rules, and never contains credentials.
2. The model calls tools. Each call is validated by its Pydantic model and dispatched by `ToolRegistry.dispatch`. Exceptions are turned into safe messages.
3. `execute_read_only_sql` → `QueryExecutor.run`:
   - `SQLValidator.validate` parses the SQL with sqlglot and checks:
     - there is a single statement;
     - the statement is a `SELECT` or set operation;
     - no forbidden nodes, comments, dollar-quoting or non-ASCII code appear;
     - the tables are on the approved list, and are rewritten to be fully qualified;
     - the functions are on the approved list;
     - a row cap is added.
   - The query then runs inside `SET TRANSACTION READ ONLY` with `SET LOCAL statement_timeout`, and is rolled back afterwards.
   - The query and its verdict are written to the audit log.
4. Results are stored in the session as `ResultRecord`s, each with a **recipe**: the original SQL or CSV name, plus any replayable analysis steps. The model receives only a summary: row count, columns, at most `LLM_MAX_SAMPLE_ROWS` sample rows (geometry masked), and numeric aggregates.
5. Charts are built from a `ChartSpec` by `visualisations/charts.py`, and the selector rejects charts that would not mean anything. The model refers to charts by ID, and the UI renders them.
6. The final answer goes through `agents/grounding.py`. Any number in the answer that does not appear in a tool output (allowing for rounding, percentages and k/m suffixes) is shown to the user as **unverified**.
7. A `HistoryEntry` is saved, containing the question, sources, SQL, methods, chart titles, warnings and errors. No rows are saved.

## Why these choices

- **Transparent tool calling.** The loop uses the official OpenAI SDK and Chat Completions directly, with no framework: about 200 lines of auditable code.
- **SQLite for app state.** The analysis role is deliberately unable to write to PostgreSQL.
- **Dashboards store recipes, not data.** Refreshing re-runs validated SQL, or re-reads the CSV, and then replays the analysis transforms. Filters are applied to the base data through quoted identifiers and bound parameters.
- **Plotly for charts.** In Phase 3, Plotly MapLibre maps will be used too, so there is one rendering and export path.
- **pandas.** It interoperates directly with GeoPandas and statsmodels.

## Extending

- **New tool:** add a Pydantic args model and a handler decorated with `@registry.register(...)` in `tools/`.
- **New PostgreSQL function the agent may call:** add it to `EXTRA_ALLOWED_FUNCTIONS` in `security/allowlists.py`. Only add read-only functions.
- **New chart type:** add a builder to `_BUILDERS` in `visualisations/charts.py`, and a rule to `chart_selector.py`.
- **Another LLM provider:** implement the `LLMClient` protocol in `agents/llm.py`.
