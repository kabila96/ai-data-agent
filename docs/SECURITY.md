# Security model

Security is enforced in code and in the database. The system prompt asks the model to behave well, but nothing depends on it doing so.

## Defence in depth

| # | Layer | Control | Where |
|---|---|---|---|
| 1 | Database role | `powell_agent_ro` is the login role and inherits from the `NOLOGIN` role `powell_readonly`. It has:<br>• `SELECT` only, on the approved schemas<br>• no `CREATE`, no write grants, and no membership of privileged built-in roles<br>• `default_transaction_read_only=on`, `statement_timeout=30s`, `idle_in_transaction_session_timeout=60s`, `lock_timeout=5s`<br>• a connection limit of 10 | `setup_readonly_user.sql` |
| 2 | Startup self-check | The app refuses to use a connection that is superuser, that can create objects, that holds any write grant, that belongs to `pg_write_all_data` or similar roles, or that is not read-only. | `database/engine.py` `security_self_check` |
| 3 | Session | Every connection is opened with `-c default_transaction_read_only=on`. Every query runs in `SET TRANSACTION READ ONLY`, with `SET LOCAL statement_timeout`, and is rolled back afterwards. | `database/engine.py`, `database/executor.py` |
| 4 | SQL validator | Parsing with the sqlglot AST enforces each of the following:<br>• one statement only<br>• a `SELECT` or `UNION` root<br>• no DML or DDL anywhere, including data-modifying CTEs, `SELECT INTO` and `FOR UPDATE`<br>• comments rejected rather than stripped<br>• no dollar-quoting<br>• no non-ASCII outside string literals<br>• tables on the approved list, rewritten as `"schema"."table"`<br>• `pg_catalog` and `information_schema` blocked<br>• functions on an allowlist, with a denylist (`pg_sleep`, `pg_read_file`, `dblink*`, `lo_*`, `set_config`, `nextval`, …)<br>• a row cap that is always applied | `security/sql_validator.py`, `security/allowlists.py` |
| 5 | Parameters | Dashboard filters and UI values are bound with SQLAlchemy parameters, and column names are quoted identifiers. Values are never formatted into SQL. | `dashboards/service.py` `build_where`, `QueryExecutor.run_wrapped` |
| 6 | Model isolation | The model can only call the 17 registered tools, and their arguments are validated. It never receives credentials, hostnames, connection strings or geometry. Sample rows are capped. | `tools/`, `agents/prompts.py`, `tools/context.py` `frame_summary` |
| 7 | Human approval | Joins only run after the user approves the exact key pair in the UI. Approvals are held in session state that the model cannot write. | `tools/data_tools.py`, `pages/chat.py` |
| 8 | Secrets | Secrets live in `.env` (git-ignored) and are held as `SecretStr`. The connection URL is built with `URL.create`, and `hide_parameters=True` is set. A log filter redacts DSNs, `password=` assignments, `sk-` keys and the literal secret values. | `config.py`, `security/redaction.py` |
| 9 | Errors | Users and the model see classified, single-line messages. Full tracebacks go only to `app_data/logs/app.log`, redacted. The Streamlit setting `showErrorDetails = "none"` is applied. | `security/errors.py`, `.streamlit/config.toml` |
| 10 | Audit | Every validated, rejected, executed or failed query is recorded with its source, verdict, row count and timing. Row data is not recorded. | `storage/app_store.py` |
| 11 | Network | Streamlit binds to `localhost`, and XSRF protection is on. | `.streamlit/config.toml` |

The database role is the real security boundary. The test `tests/test_database_live.py::test_database_role_blocks_writes_even_without_validator` sends raw `DROP`, `DELETE`, `INSERT`, `UPDATE`, `CREATE`, `TRUNCATE`, `SET ROLE` and `pg_read_file` statements through the application's engine with the validator bypassed. It checks that PostgreSQL rejects every one of them.

## Hiding sensitive tables inside an approved schema

1. Run `REVOKE SELECT ON schema.table FROM powell_readonly;`.
2. List the table in `BLOCKED_TABLES` in `.env`.

Each step hides the table on its own, so use both. A future table created by the admin role will be readable through the default privileges. Revoke access to it explicitly if it is sensitive.

## Data sent to OpenAI

The model receives the following:
- schema and column metadata, and your data-dictionary text;
- row counts and numeric aggregates;
- up to `LLM_MAX_SAMPLE_ROWS` rows per result, with strings truncated and geometry masked.

Set `LLM_MAX_SAMPLE_ROWS=0` to send no row values at all; the agent then reasons only from aggregates and metadata. Check your organisation's policy before analysing personal data.

## What is stored locally

`app_data/app_store.sqlite3` contains:
- questions;
- SQL text;
- method names;
- chart titles;
- warnings and errors;
- data-dictionary entries;
- dashboard recipes.

Dashboards built on a CSV save a copy of that CSV under `saved_dashboards/data/`, so that they can be refreshed later. Delete those copies if the file is sensitive.
