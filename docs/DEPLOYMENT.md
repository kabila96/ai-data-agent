# Moving online (later)

The local design carries over. Change these things before you expose the app beyond `localhost`.

## Required before going online

1. **Authentication.** Put the app behind an identity provider. Options:
   - Streamlit's built-in OIDC (`st.login`, configured in `.streamlit/secrets.toml`);
   - an authenticating reverse proxy, such as Azure App Service Authentication, Cloudflare Access or oauth2-proxy.

   Never publish the app without authentication.
2. **TLS everywhere.**
   - Use HTTPS at the proxy.
   - Set `PG_SSLMODE=verify-full` with the server's CA certificate.
3. **Secrets manager.**
   - Move `PG_PASSWORD` and `OPENAI_API_KEY` from `.env` into Azure Key Vault, AWS Secrets Manager or the platform's secret settings. Pydantic-settings reads these from environment variables, so no code change is needed.
   - Rotate both secrets when you move.
4. **Network.**
   - Keep PostgreSQL private. Use a private endpoint or VNet, and allow only the app's egress IP in `pg_hba.conf`.
   - Remove `address = "localhost"` from `.streamlit/config.toml` only when the app sits behind the proxy.
5. **Multi-user state.**
   - Move the SQLite store to a dedicated Postgres schema owned by a *separate* writer role, never `powell_agent_ro`.
   - Add a `user_id` to history, dashboards and the dictionary.
   - Keep the analysis role read-only.
6. **Per-user data access.**
   - If different users may see different data, use PostgreSQL row-level security policies, or one read-only role per tenant.
   - Pass the user's identity in a `SET LOCAL app.user_id` executed by the application, not by the model.

## Recommended

- **Container.** Use a `python:3.12-slim` image with `pip install -r requirements.txt`, run `streamlit run app.py --server.port $PORT`, and run as a non-root user.
- **Hosting.** Azure Container Apps or App Service, AWS App Runner or ECS, or Google Cloud Run. Pair it with a managed PostgreSQL that supports PostGIS, such as Azure Database for PostgreSQL Flexible Server, Amazon RDS or Cloud SQL.
- **Rate limits and cost controls.** Add a per-user daily cap on agent steps, and log token usage per request.
- **Observability.** Ship `app.log` to the platform's log service. The redaction filter already applies to every handler.
- **Data-processing agreement.** Check OpenAI's data-use terms for your organisation. Set `LLM_MAX_SAMPLE_ROWS=0` for sensitive datasets.
- **Backups.** Back up the app metadata schema. It holds dashboard recipes and the dictionary, and no analysis data.
- **CI.** Run `pytest` on every push, and the live tests against a disposable PostGIS container (`postgis/postgis:18-3.6`) seeded with `sample_data/demo_seed_sales.sql`.

## Commercial readiness checklist

- [ ] SSO and role-based page access
- [ ] Tenant isolation (RLS or separate databases)
- [ ] Audit-log export and retention policy
- [ ] Privacy notice covering LLM processing
- [ ] Penetration test of the SQL validator and the prompt-injection paths (the role boundary makes injection low-impact, but test it)
- [ ] Load test of concurrent sessions against the connection pool and the role's `CONNECTION LIMIT`
