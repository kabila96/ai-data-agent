# Powell Analytics Agent

A locally deployable AI data-analysis assistant for the **`powell_analytics`** PostgreSQL/PostGIS database and uploaded CSV files. You ask questions in plain English. The agent inspects metadata, writes read-only SQL that is validated in code, runs it through a restricted database role, analyses the result in Python, and then explains it with charts, evidence, assumptions and data-quality warnings. Useful charts can be saved as dashboards that refresh from their original sources.

> **Status: Phase 3.** Everything from the core MVP plus PostGIS layer checks, spatial-join diagnostics, interactive maps (choropleth, point, proportional-symbol, animated), GeoJSON export, validated forecasting, anomaly detection and regression. See [Verification status](#verification-status).

---

## 1. Quick start (Windows, conda)

Prerequisites:
- Anaconda or Miniconda
- PostgreSQL 18 with PostGIS 3.6
- An OpenAI API key

### Steps

1. **Create the read-only database role.** Run this once, as `postgres`, from the project folder:

   ```bat
   psql -U postgres -d powell_analytics -v agent_password="'Choose-A-Strong-Password'" -f setup_readonly_user.sql
   ```

   If you use pgAdmin instead, open the Query Tool on `powell_analytics`. Replace `:agent_password` in the script with a quoted password, then run it.

2. **Optional: load the demo data** into your `training` schema. The sales script creates `training.demo_*` retail tables; the GIS script creates two small Birmingham layers in different CRSs (with deliberate geometry problems) for the map and spatial-check examples.

   ```bat
   psql -U postgres -d powell_analytics -f sample_data\demo_seed_sales.sql
   psql -U postgres -d powell_analytics -f sample_data\demo_seed_gis.sql
   ```

3. **Install the environment.** Run this in an **Anaconda Prompt** from the project folder:

   ```bat
   scripts\setup_windows.bat
   notepad .env
   ```

   In `.env`, set `PG_PASSWORD` to the password you chose in step 1, and set `OPENAI_API_KEY`.

4. **Check the setup.** This prints no secrets. Add `--openai` to send one small test request to the model.

   ```bat
   scripts\check_setup.bat
   ```

5. **Start the app.** The browser opens at http://localhost:8501.

   ```bat
   scripts\run_app.bat
   ```

To stop the app, press `Ctrl+C` in the prompt window. To start it again later, run `scripts\run_app.bat`.

### Manual equivalent

```bat
conda env create -f environment.yml
conda activate ai_data_agent
copy .env.example .env
python scripts\check_setup.py
python -m streamlit run app.py
```

## 2. Pages

| Page | What it does |
|---|---|
| **Chat with Data** | Ask questions about the selected sources (database and/or CSVs). Each answer shows: <br>• a direct answer and the evidence behind it<br>• charts<br>• result tables<br>• the executed SQL, in a collapsible section<br>• the analysis steps taken<br>• warnings<br><br>Follow-up questions keep the conversation's context. Joins wait for you to approve them with a button. Results the agent offers for export get download buttons. |
| **Database Explorer** | Shows connection and security status, plus the approved schemas, tables and views. For each table you can see: <br>• columns and types<br>• primary and foreign keys<br>• geometry type and SRID<br>• a row preview<br><br>You can also search table and column names, and keep a **data dictionary** of descriptions and units. The agent reads that dictionary. |
| **CSV Analysis** | Upload one or more CSVs, then: <br>• review and correct the detected column types<br>• profile data quality: missing values, duplicates, invalid dates, outliers, inconsistent labels, mixed currencies<br>• view descriptive statistics and trends<br>• compare the CSV with a database table, or join them on a key that you approve |
| **Visualisation Workspace** | Build or refine any of 10 chart types from any result or CSV. The app suggests a chart, and will tell you when a table is better than a chart. Every chart carries a title, units, source, filters, date range, an interpretation and any warnings. |
| **Dashboard Builder** | Open saved dashboards. You can: <br>• rename components, change their width and reorder them<br>• add filters (date range, multi-select, numeric range)<br>• refresh every component from its original source<br>• export the data (ZIP of CSVs), a printable HTML summary (use the browser's Print → Save as PDF) or the configuration (JSON) |
| **Analysis History** | Every question is listed with its sources, SQL, methods, charts, warnings and errors, together with the full SQL audit log. No credentials or row-level data are stored. |

## 3. Example questions

**Database (demo data):**
- What tables are available in the database?
- Describe the columns in the demo_sales table.
- What were total sales by branch last month?
- Show monthly revenue and gross-profit trends.
- Which products experienced declining sales?
- Identify unusual transactions, missing values and possible duplicates in demo_sales.
- Which branches had the most stockouts this year?
- Create a dashboard showing sales, profit, stockouts and branch performance.
- Explain the most important findings in plain language.

**CSV:** upload `sample_data/branch_sales_upload.csv` and `sample_data/branch_targets.csv`, then try:
- Check this file for data-quality problems.
- Compare the uploaded targets with the demo_branches table.
- Join the sales upload to the targets by branch. (You will be asked to approve the join. Note that the targets file contains a deliberately duplicated key.)

**GIS** (demo layers, or your own `gis` tables):
- Check training.demo_wards and training.demo_health_facilities before a spatial join.
- How many health facilities are located in each ward?
- Create a choropleth map of health facilities per 10,000 residents by ward.
- Show the hospitals as a proportional-symbol map sized by beds.
- How far is each facility from the nearest hospital, in metres?

**Forecasting and anomalies:**
- Forecast daily revenue for the next 30 days.
- Forecast monthly units for Camden for the next 6 months.
- Identify unusual transactions in demo_sales.

## 4. Project structure

```text
ai_data_agent/
├── app.py                      Streamlit entry point and navigation
├── config.py                   Typed settings from .env (secrets as SecretStr)
├── services.py                 Wiring: store, engine, security self-check, session context
├── pages/                      chat · database_explorer · csv_analysis · visualisation_workspace ·
│                               dashboard_builder · analysis_history
├── components/                 Streamlit state helpers and reusable UI blocks
├── agents/                     LLM adapter, system prompt, agent loop, number-grounding check
├── tools/                      17 typed agent tools + registry (the model can call nothing else)
├── database/                   Engine, fixed catalog queries, validated read-only executor
├── security/                   sqlglot SQL validator, allow/deny lists, log redaction, safe errors
├── analysis/                   CSV loading, profiling, joins, statistics, replayable transforms
├── visualisations/             Chart selector, Plotly chart builders, theme
├── dashboards/                 Dashboard save/refresh/filters/export
├── geospatial/                 (Phase 3) CRS handling, spatial QA, maps
├── storage/                    SQLite store: history, audit log, data dictionary, dashboards
├── models/                     Pydantic models
├── tests/                      Pytest suite (offline + live-database tests)
├── sample_data/                Demo CSVs and demo_seed_sales.sql
├── saved_dashboards/           JSON copies of dashboard configurations (recipes, no data)
├── scripts/                    Windows setup/run/test scripts, check_setup.py, sample-data generator
├── docs/                       ARCHITECTURE · SECURITY · TROUBLESHOOTING · DEPLOYMENT
├── setup_readonly_user.sql     Creates the restricted read-only role
├── requirements.txt · environment.yml · .env.example · .gitignore · pyproject.toml
```

Changes from the original brief:
- `services.py` and `storage/` were added. Application state goes in a local SQLite file, because the database role is read-only and cannot write history or dashboards.
- `scripts/` and `docs/` were added.
- Pages are registered with `st.navigation`, which allows page names and groups to be customised.

## 5. Testing

```bat
scripts\run_tests.bat
```

To include the live tests against your database, use the read-only role's credentials:

```bat
set TEST_PG_PASSWORD=<read-only password>
set TEST_PG_HOST=localhost
set TEST_PG_SUPERUSER_PASSWORD=<postgres password>
scripts\run_tests.bat
```

The live tests need the demo tables. `TEST_PG_SUPERUSER_PASSWORD` is optional; when set, it also checks that the app refuses a superuser connection.

The suite covers the following:
- The SQL validator, including 50 destructive and injection queries that must be rejected.
- Raw writes and privilege escalation sent straight to PostgreSQL with the validator bypassed, which the database role must reject.
- Statement timeouts, row caps, parameterised filters, metadata restricted to approved schemas, blocked tables, and the audit log.
- CSV type inference and overrides, plus profiling of seeded data-quality issues.
- Join inflation and enforcement of join approval.
- Statistical guards against unsafe tests.
- All 10 chart types and the chart selector.
- Dashboard persistence across a restart, refresh with filters, and export.
- The agent loop (using a scripted model), detection of fabricated numbers, the step limit, and the OpenAI adapter.
- Secret redaction.

## 6. Verification status

| Area | Status |
|---|---|
| On your Windows PC (PostgreSQL 18.4, PostGIS 3.6.2, `gpt-5.6-luna`) | **Verified 9 Oct 2026** with `scripts\verify.bat`: setup checks all passed, model replies and calls tools, read-only role confirmed, 171 tests passed; the remaining tests need the optional demo data and skip without it |
| SQL validator, executor, metadata, role script, demo seed | **Tested** against PostgreSQL 16 (build environment) and 18.4 (your PC) |
| CSV, profiling, joins, statistics, charts, dashboards, history, agent loop | **Tested** (pytest, 171 tests). Charts were rendered and inspected visually. |
| OpenAI calls | Adapter tested with a mocked SDK client. **Live call with `gpt-5.6-luna` still needs checking:** run `check_setup.bat --openai`. |
| Streamlit pages | Written against the Streamlit 1.65 API. **A first run on your Windows machine is still needed.** |
| PostgreSQL 18 / PostGIS 3.6 | Catalog queries use stable system views. **Not yet run against PG 18.** |
| Forecasting, anomaly detection, regression | **Tested** offline (holdout validation, baselines, intervals, IsolationForest, time-series spikes) |
| Maps and GeoJSON export | **Tested** offline; polygons rendered and inspected. Basemap tiles need internet on your machine. |
| PostGIS checks and spatial joins | SQL validated here; **live tests in `tests/test_geospatial_live.py` run on your machine** after loading `demo_seed_gis.sql` |

See `docs/` for the architecture, the security model and troubleshooting. To put the app online, follow **`docs/DEPLOY_ONLINE.md`** (GitHub + Streamlit Community Cloud + a Neon PostGIS database, with a password lock).
