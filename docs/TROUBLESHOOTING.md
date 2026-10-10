# Troubleshooting (Windows)

Start with `scripts\check_setup.bat`. It checks packages, `.env`, the database connection, the role's privileges and the OpenAI key, and it never prints secrets.

| Symptom | Likely cause | Fix |
|---|---|---|
| `'conda' is not recognized` | You are using a normal Command Prompt | Open **Anaconda Prompt** from the Start menu, or run `conda init cmd.exe` once. |
| `conda env create` fails on geopandas, pyproj or shapely | An old conda or a mixed set of channels | Run `conda update -n base conda`. Then run `conda env remove -n ai_data_agent` and run `scripts\setup_windows.bat` again. |
| *Database login failed* | Wrong `PG_PASSWORD`, or the role was never created | Re-run `setup_readonly_user.sql` with a new password, and update `.env`. |
| *Could not reach PostgreSQL* | The service is stopped, or the port is wrong | Open Services → `postgresql-x64-18` → Start. Check that `PG_PORT` matches the port in `postgresql.conf`. |
| *Connection refused by the application's security check: Role … is a SUPERUSER* | `.env` uses `postgres` | Set `PG_USER=powell_agent_ro`. |
| *Role has write privileges on N table(s)* | Earlier manual grants | Run `setup_readonly_user.sql` again. Section 5 of the script revokes everything except `SELECT`. |
| *Approved schemas not found or not readable: salesdata* | The schema name differs, or the grant is missing | Run `setup_readonly_user.sql`. Names are matched case-insensitively, so "Salesdata" works. Check `APPROVED_SCHEMAS`. |
| A table is missing in the Explorer | It was created after the grants, by a different owner | Run `setup_readonly_user.sql` again, or run `GRANT SELECT ON schema.table TO powell_readonly;`. |
| *Function 'xyz' is not on the approved function list* | The validator does not recognise that function | Add it to `EXTRA_ALLOWED_FUNCTIONS` in `security/allowlists.py`, but only if it is read-only. |
| *The query exceeded the statement timeout* | The query is too heavy | Add date filters, aggregate in SQL, or raise `STATEMENT_TIMEOUT_MS`. The role itself caps queries at 30 s. |
| *Result reached the row limit* | The query returned more than `MAX_ROWS` rows | Aggregate in SQL. Raise `MAX_ROWS` only if you really need raw rows. |
| *OpenAI rejected the API key* | The key is wrong or has been revoked | Update `OPENAI_API_KEY`. |
| *Model 'gpt-5.6-luna' is not available to this API key* | Your account does not have access to that model | Change `OPENAI_MODEL` in `.env`, then restart the app. |
| *Could not reach the OpenAI API* | A proxy or firewall is blocking it | Set the `HTTPS_PROXY` environment variable before starting the app, or use `OPENAI_BASE_URL`. |
| A CSV's dates are wrong (month and day swapped) | The file mixes date formats | Fix the type in **CSV Analysis → Column types**. ISO dates are tried first, then UK day-first dates. |
| Port 8501 is already in use | Another Streamlit instance is running | Close it, or run `python -m streamlit run app.py --server.port 8502`. |
| Blank page, or the browser does not open | The app runs headless | Browse to http://localhost:8501 yourself. |
| Something else | — | See `app_data\logs\app.log`. It contains full, redacted error details. |

## Resetting local state

Stop the app, then delete the `app_data\` folder. This removes the history, audit log and data dictionary.

Dashboards are also stored in that folder. Their JSON copies stay in `saved_dashboards\`.
