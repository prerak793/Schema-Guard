# SchemaGuard

SchemaGuard is an AI agent that listens for GitHub Pull Request webhooks, detects upstream database schema changes, queries DataHub's metadata lineage graph to find broken downstream dbt models, and automatically generates and opens a fix PR in the data engineering repository.

## Prerequisites

- Python 3.11+ (local development)
- A GitHub personal access token with `repo` scope
- An OpenAI API key
- (Optional) A running DataHub instance for lineage lookup
- A GitHub/GitLab/Bitbucket repository pushed to a remote (for Render deployment)

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PORT` | HTTP port. Render sets this automatically; use `8000` locally. |
| `GITHUB_TOKEN` | GitHub PAT for repo access |
| `OPENAI_API_KEY` | OpenAI key for GPT-4o |
| `DATAHUB_GMS_URL` | DataHub GraphQL endpoint (optional; falls back to mock data) |
| `DATAHUB_TOKEN` | DataHub auth token (optional) |
| `DATA_REPO_NAME` | dbt repo in `owner/repo` format |

Copy `.env.example` to `.env` for local development:

```bash
cp .env.example .env
```

---

## Deploy on Render (recommended)

SchemaGuard is designed to run as a public web service on [Render](https://render.com). No ngrok or local tunneling required.

### 1. Push to GitHub

```bash
git add .
git commit -m "Add SchemaGuard"
git push origin main
```

### 2. Create the service from Blueprint

Open the Render Blueprint flow with your repo URL:

```
https://dashboard.render.com/blueprint/new?repo=https://github.com/<your-username>/<your-repo>
```

Render reads `render.yaml` from the repo root and provisions a **Web Service** with:

- **Build:** `pip install -r requirements.txt`
- **Start:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Health check:** `GET /health`

### 3. Set secret environment variables

In the Render Dashboard (or during Blueprint setup), fill in:

- `GITHUB_TOKEN`
- `OPENAI_API_KEY`
- `DATA_REPO_NAME` (e.g. `your-username/data-repo`)
- `DATAHUB_GMS_URL` (optional)
- `DATAHUB_TOKEN` (optional)

Click **Apply** to deploy. Once live, your service URL will look like:

```
https://schemaguard.onrender.com
```

Verify it is healthy:

```bash
curl https://schemaguard.onrender.com/health
```

### 4. Connect the GitHub webhook

In the **schema-change repository** (the repo whose PRs trigger SchemaGuard):

1. Go to **Settings → Webhooks → Add webhook**
2. **Payload URL:** `https://schemaguard.onrender.com/webhook`
3. **Content type:** `application/json`
4. **Events:** Pull requests
5. Save

When a PR containing `ALTER TABLE` or `RENAME COLUMN` is opened or updated, SchemaGuard will:

1. Fetch the PR diff
2. Query DataHub for downstream dbt models (falls back to `stg_users` if DataHub is unavailable)
3. Use GPT-4o to generate updated SQL and schema YAML
4. Open a fix PR in `DATA_REPO_NAME`

> **Free tier note:** Render free web services spin down after ~15 minutes of inactivity. The first webhook after idle may take 30–60 seconds while the service cold-starts. Upgrade to a paid plan for always-on uptime.

### Manual deploy (without Blueprint)

If you prefer the Dashboard over Blueprint:

1. **New → Web Service** → connect your repo
2. **Runtime:** Python 3
3. **Build command:** `pip install -r requirements.txt`
4. **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Health check path:** `/health`
6. Add the environment variables listed above

---

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
pip install -r requirements.txt
uvicorn main:app --reload
```

The webhook endpoint is available at `http://localhost:8000/webhook`.

```bash
curl http://localhost:8000/health
```
