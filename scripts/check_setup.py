"""Pre-flight check: configuration, packages, database role and OpenAI access.

    python scripts/check_setup.py            # config + database checks
    python scripts/check_setup.py --openai   # also make one tiny OpenAI request

Prints no secrets.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
REQUIRED = ["streamlit", "sqlalchemy", "psycopg", "sqlglot", "pandas", "plotly", "pydantic_settings", "openai",
            "scipy", "statsmodels", "sklearn", "geopandas", "shapely", "pyproj"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openai", action="store_true", help="send one small request to verify the model")
    args = parser.parse_args()
    failures = 0

    print(f"{OK} Python {sys.version.split()[0]}" if sys.version_info >= (3, 12)
          else f"{FAIL} Python 3.12+ required, found {sys.version.split()[0]}")
    for name in REQUIRED:
        try:
            module = importlib.import_module(name)
            print(f"{OK} {name} {getattr(module, '__version__', '')}")
        except Exception as exc:
            failures += 1
            print(f"{FAIL} {name} not importable ({type(exc).__name__}). Run: pip install -r requirements.txt")

    env = ROOT / ".env"
    print(f"{OK} .env found" if env.exists() else f"{FAIL} .env missing: copy .env.example to .env")
    from config import get_settings

    settings = get_settings()
    print(f"{OK} Database target {settings.pg_user}@{settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
    print(f"{OK} Approved schemas: {', '.join(settings.approved_schemas)}")
    if settings.pg_user.lower() in ("postgres", "admin", "root"):
        failures += 1
        print(f"{FAIL} PG_USER is '{settings.pg_user}'. Use the read-only role from setup_readonly_user.sql.")

    from services import build_services

    services = build_services(settings)
    if services.database_ready:
        r = services.security
        print(f"{OK} Connected. PostgreSQL {r.server_version.split()[0]}, PostGIS {r.postgis_version or 'not installed'}")
        print(f"{OK} Role is restricted (no superuser, read-only session, no write grants)")
        for schema in services.metadata.list_schemas():
            n = len(services.metadata.list_tables(schema))
            print(f"{OK} Schema {schema}: {n} readable table(s)/view(s)")
        for notice in services.notices:
            print(f"{WARN} {notice}")
    else:
        failures += 1
        print(f"{FAIL} Database: {services.db_error}")

    if not settings.openai_configured:
        print(f"{WARN} OPENAI_API_KEY not set: the chat agent will be disabled (other pages still work).")
    else:
        import os

        from dotenv import dotenv_values

        def fingerprint(key: str | None) -> str:
            return f"{key[:8]}...{key[-4:]} (length {len(key)})" if key and len(key) > 12 else "not set"

        used = settings.openai_api_key.get_secret_value()
        in_file = (dotenv_values(env).get("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
        in_windows = os.environ.get("OPENAI_API_KEY")
        print(f"{OK} OpenAI key in use: {fingerprint(used)}; model {settings.openai_model}")
        print(f"       .env file:            {fingerprint(in_file)}")
        print(f"       Windows env variable: {fingerprint(in_windows)}")
        if in_windows and in_windows.strip() != used:
            print(f"{WARN} A different OPENAI_API_KEY is set in Windows environment variables. "
                  "The app now uses the .env value; consider deleting the Windows variable.")
        if args.openai:
            from agents.llm import build_llm
            from security.errors import AppError
            from tools import registry

            try:
                llm = build_llm(settings)
                reply = llm.complete([{"role": "user", "content": "Reply with the single word: ready"}], [])
                print(f"{OK} Model replied ({settings.openai_api_style} API): {(reply.content or '').strip()[:40]}")
                # The agent needs tool calling, so test that too.
                reply = llm.complete([{"role": "user", "content": "Call the list_available_sources tool."}],
                                     registry.schemas())
                if reply.tool_calls:
                    print(f"{OK} Tool calling works (model called {reply.tool_calls[0].name})")
                else:
                    print(f"{WARN} Model answered without calling a tool: {(reply.content or '')[:80]}")
            except AppError as exc:
                failures += 1
                print(f"{FAIL} OpenAI: {exc.user_message}")

    print("\nAll checks passed." if not failures else f"\n{failures} problem(s) found. See docs/TROUBLESHOOTING.md.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
