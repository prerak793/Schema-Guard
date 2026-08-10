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

## Setup via GitHub Actions (Recommended — Zero Hosting Required!)

The easiest way to use SchemaGuard is to run it as a **GitHub Action** directly in your repository. This means you don't need to deploy any servers, manage webhooks, or pay for hosting.

### 1. Add Secrets to your Repository
Go to your database/schema repository on GitHub -> **Settings -> Secrets and variables -> Actions -> New repository secret**.

Add the following secrets:
* `GITHUB_TOKEN`: A Personal Access Token (PAT) with `repo` scope to open the fix PR.
* `OPENAI_API_KEY`: Your OpenAI API key for GPT-4o.
* `DATA_REPO_NAME`: The name of your dbt data engineering repository (e.g., `prerak793/data-models`).
* `DATAHUB_GMS_URL`: (Optional) Your DataHub GraphQL endpoint.
* `DATAHUB_TOKEN`: (Optional) Your DataHub token.

### 2. Copy the Workflow File
In the repository where your database schema changes happen, create a new file at `.github/workflows/schemaguard.yml` and copy the workflow from this repository into it.

That's it! Now, whenever someone opens a Pull Request with a database change, GitHub Actions will spin up, analyze the diff, and open a fix PR in your data repository automatically.

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
