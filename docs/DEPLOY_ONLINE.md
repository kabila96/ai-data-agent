# Put the app online (GitHub + Streamlit Community Cloud + Neon)

GitHub stores the code. It cannot run a Python app, so the live site runs on **Streamlit Community Cloud**, which deploys straight from your GitHub repository. Your PostgreSQL runs on your own PC, which the cloud app cannot reach, so the online version needs a **cloud database**. **Neon** has a free plan and supports PostGIS.

```text
GitHub repo ──deploys──▶ Streamlit Community Cloud (the app)
                                   │  read-only login over SSL
                                   ▼
                          Neon PostgreSQL + PostGIS (demo / public data only)
```

Put only data that you are happy to make public into the cloud database. Always set `APP_PASSWORD`, so that strangers cannot use your OpenAI credit.

## 1. Push the code to GitHub

1. **Create an empty repository.** Go to https://github.com/new.
   - Name it `ai-data-agent`.
   - Choose Public for a portfolio, or Private.
   - Do **not** add a README, .gitignore or licence.
2. **Install Git**, if `git --version` fails in the Anaconda Prompt. Get it from https://git-scm.com/download/win and accept the defaults.
3. **Commit the project.** In the project folder, run:
   ```bat
   git init
   git add .
   git status
   ```
   Check the list that `git status` prints. It must **not** include `.env`, `app_data/` or any `.zip` file. If it does, stop and fix `.gitignore` first. Then run:
   ```bat
   git commit -m "AI data-analysis agent for PostgreSQL/PostGIS"
   git branch -M main
   git remote add origin https://github.com/<your-username>/ai-data-agent.git
   git push -u origin main
   ```
   A browser window opens the first time so that you can sign in to GitHub.
4. **Decide about your photo.** `assets/logo.jpg` is your photo. It is uploaded too, so delete it before `git add` if you would rather not publish it.

## 2. Create the cloud database (Neon)

1. Sign up at https://neon.tech, then create a project. Pick the region nearest to you, for example London or Frankfurt.
2. In the Neon dashboard, open **SQL Editor** and run:
   ```sql
   CREATE EXTENSION IF NOT EXISTS postgis;
   CREATE SCHEMA IF NOT EXISTS training;
   ```
3. **Load the demo data.** Click **Connect** and copy the connection string (`postgresql://…neon.tech/neondb?sslmode=require`). Then, from the project folder, run:
   ```bat
   "C:\Program Files\PostgreSQL\18\bin\psql.exe" "<connection string>" -f sample_data\demo_seed_sales.sql
   "C:\Program Files\PostgreSQL\18\bin\psql.exe" "<connection string>" -f sample_data\demo_seed_gis.sql
   "C:\Program Files\PostgreSQL\18\bin\psql.exe" "<connection string>" -v agent_password="'<new strong password>'" -f setup_readonly_user.sql
   ```
   The last command creates the read-only login `powell_agent_ro` in the cloud. Give it a different password from the one on your PC.
4. **Note the host.** It is the part of the connection string between `@` and `/`, for example `ep-xxx.eu-west-2.aws.neon.tech`. You need it for the secrets in the next section.

## 3. Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub. Then choose **Create app**, then **Deploy a public app from GitHub**.
2. Fill in:
   - **Repository:** `<your-username>/ai-data-agent`
   - **Branch:** `main`
   - **Main file:** `app.py`
   - **App URL:** pick a name, for example `powell-data-agent`
3. Open **Advanced settings**:
   - Choose **Python 3.12**.
   - Paste the secrets below. This is TOML format, so keep the quotes.
   ```toml
   PG_HOST = "ep-xxx.eu-west-2.aws.neon.tech"
   PG_PORT = "5432"
   PG_DATABASE = "neondb"
   PG_USER = "powell_agent_ro"
   PG_PASSWORD = "<the cloud read-only password>"
   PG_SSLMODE = "require"
   APPROVED_SCHEMAS = "training,public"
   OPENAI_API_KEY = "sk-proj-..."
   OPENAI_MODEL = "gpt-5.6-luna"
   APP_PASSWORD = "<a password you give to people you trust>"
   ```
4. Click **Deploy**. The first build installs everything and takes a few minutes.
5. **Test the site.** Open the URL and enter `APP_PASSWORD`. Then try "What tables are available in the database?" and "Create a choropleth map of health facilities per ward."

## 4. Keep it safe and cheap

- **Limit OpenAI spending.** In the OpenAI dashboard, open **Limits** and set a monthly budget.
- **Change passwords through secrets only.** To change a password, edit the secrets in Streamlit Cloud (open the app, then **Settings → Secrets**). Never put a password in a file in the repository.
- **History and dashboards do not last.** On Streamlit Cloud they live in temporary storage, so a redeploy or restart clears them. That is fine for a demo; see `docs/DEPLOYMENT.md` for a permanent setup.
- **Update the site with a push.** Commit and run `git push`, and Streamlit Cloud redeploys automatically.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Database login failed" online | Check `PG_PASSWORD` in the secrets, and that `setup_readonly_user.sql` ran on Neon. |
| "Could not reach PostgreSQL" | Check `PG_HOST` (no `postgresql://` prefix) and `PG_SSLMODE = "require"`. |
| The build fails while installing packages | Open **Manage app** to read the log, and send it to me. The usual cause is the Python version: choose 3.12. |
| No tables are listed | Set `APPROVED_SCHEMAS` to the schemas that exist on Neon, for example `training,public`. |
| The app asks for no password | `APP_PASSWORD` is missing from the secrets. |
