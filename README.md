# SchemaGuard

SchemaGuard is an AI agent that listens for GitHub Pull Request webhooks, detects upstream database schema changes, queries DataHub's metadata lineage graph to find broken downstream dbt models, and automatically generates and opens a fix PR in the data engineering repository.

## Prerequisites

- Python 3.11+ (local development)
- A GitHub personal access token with `repo` scope
- An OpenAI API key
- (Optional) A running DataHub instance for lineage lookup

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PORT` | HTTP port. Set automatically by the host; use `8000` locally. |
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

## Deploy on Koyeb (free — recommended)

[Koyeb](https://www.koyeb.com) offers a **permanent free tier**: 1 web service, 512 MB RAM, no credit card required.

### 1. Create a free web service

1. Sign up at [koyeb.com](https://www.koyeb.com) (no credit card needed)
2. Click **Create Web Service**
3. Connect GitHub → select **prerak793/Schema-Guard**
4. **Instance type:** choose **Free** (not Eco/Starter)
5. **Build command:**
   ```
   pip install -r requirements.txt
   ```
6. **Run command:**
   ```
   uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
7. **Port:** `8000` (Koyeb maps this automatically)
8. **Health check:** `/health`
9. Add environment variables:
   - `GITHUB_TOKEN`
   - `OPENAI_API_KEY`
   - `DATA_REPO_NAME`
   - `DATAHUB_GMS_URL` (optional)
   - `DATAHUB_TOKEN` (optional)
10. Click **Deploy**

Your URL will look like:

```
https://schemaguard-<your-org>.koyeb.app
```

Verify:

```bash
curl https://schemaguard-<your-org>.koyeb.app/health
```

### 2. Connect the GitHub webhook

In your **schema-change repository** → **Settings → Webhooks → Add webhook**:

| Field | Value |
|-------|-------|
| **Payload URL** | `https://schemaguard-<your-org>.koyeb.app/webhook` |
| **Content type** | `application/json` |
| **Events** | Pull requests |

When a PR containing `ALTER TABLE` or `RENAME COLUMN` is opened or updated, SchemaGuard will:

1. Fetch the PR diff
2. Query DataHub for downstream dbt models (falls back to `stg_users` if unavailable)
3. Use GPT-4o to generate updated SQL and schema YAML
4. Open a fix PR in `DATA_REPO_NAME`

> **Free tier note:** Koyeb scales to zero after ~1 hour of no traffic. The first webhook after idle may take a few seconds to cold-start. GitHub allows 10 seconds for webhook delivery, which is usually enough.

---

## Deploy on Render (free, but pick the right plan)

Render **does** have a free tier ($0/month), but the Blueprint flow often defaults to **Starter ($7/mo)**. You must explicitly choose **Free**.

### Option A: Manual deploy (easier to stay free)

1. [dashboard.render.com](https://dashboard.render.com) → **New → Web Service**
2. Connect **prerak793/Schema-Guard**
3. **Instance type:** select **Free** ($0) — not Starter
4. **Build:** `pip install -r requirements.txt`
5. **Start:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. **Health check path:** `/health`
7. Add env vars and deploy

Webhook URL: `https://<service-name>.onrender.com/webhook`

### Option B: Blueprint

```
https://dashboard.render.com/blueprint/new?repo=https://github.com/prerak793/Schema-Guard
```

The repo includes `render.yaml` with `plan: free`. **Before clicking Apply**, confirm each service shows **Free — $0/month**, not Starter ($7).

> Render free services spin down after 15 min idle (~30–60s cold start).

---

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
pip install -r requirements.txt
uvicorn main:app --reload
```

```bash
curl http://localhost:8000/health
```

Webhook endpoint: `http://localhost:8000/webhook`
