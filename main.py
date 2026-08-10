"""
SchemaGuard — AI agent for detecting upstream schema changes and auto-fixing downstream dbt models.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from github import Github, GithubException, InputGitTreeElement
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("schemaguard")

PORT = int(os.environ.get("PORT", "8000"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DATAHUB_GMS_URL = os.environ.get("DATAHUB_GMS_URL", "")
DATAHUB_TOKEN = os.environ.get("DATAHUB_TOKEN", "")
DATA_REPO_NAME = os.environ.get("DATA_REPO_NAME", "")

SCHEMA_CHANGE_PATTERN = re.compile(
    r"alter\s+table|rename\s+column",
    re.IGNORECASE,
)

DATAHUB_LINEAGE_QUERY = """
query getDownstreamLineage($urn: String!) {
  entity(urn: $urn) {
    urn
    type
    ... on Dataset {
      downstream: lineage(input: { direction: DOWNSTREAM, maxHops: 5 }) {
        relationships {
          entity {
            urn
            type
            ... on Dataset {
              name
              platform {
                name
              }
              properties {
                name
                customProperties {
                  key
                  value
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

MOCK_DOWNSTREAM_MODELS = ["stg_users"]


# ---------------------------------------------------------------------------
# A. Pydantic Models for Structured LLM Output
# ---------------------------------------------------------------------------


class DBTModelUpdate(BaseModel):
    """Structured output from the LLM when generating dbt model fixes."""

    reasoning: str = Field(
        description=(
            "Step-by-step explanation of what schema change was detected "
            "and how the dbt model was updated to remain compatible."
        ),
    )
    updated_sql: str = Field(
        description=(
            "Complete, valid ANSI SQL for the dbt model. "
            "Use explicit column aliases when upstream columns were renamed. "
            "Do not include markdown fences."
        ),
    )
    updated_yaml: str = Field(
        description=(
            "Complete dbt schema.yml entry for this model using valid YAML "
            "with 2-space indentation. Include version, models, name, "
            "description, and columns with names and descriptions."
        ),
    )


# ---------------------------------------------------------------------------
# B. DataHub Lineage Client
# ---------------------------------------------------------------------------


def _build_dataset_urn(table_name: str) -> str:
    """Build a best-effort DataHub dataset URN from a bare table name."""
    normalized = table_name.strip().lower()
    if normalized.startswith("urn:"):
        return normalized
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,public.{normalized},PROD)"


def _extract_dbt_model_names(lineage_payload: dict[str, Any]) -> list[str]:
    """Parse DataHub lineage response and return downstream dbt model names."""
    models: list[str] = []
    entity = lineage_payload.get("data", {}).get("entity") or {}
    downstream = entity.get("downstream") or {}
    relationships = downstream.get("relationships") or []

    for rel in relationships:
        rel_entity = rel.get("entity") or {}
        urn: str = rel_entity.get("urn", "")
        name: str = rel_entity.get("name") or ""

        props = rel_entity.get("properties") or {}
        custom_props = props.get("customProperties") or []
        custom_map = {cp.get("key"): cp.get("value") for cp in custom_props}

        dbt_model = custom_map.get("dbt_model") or custom_map.get("dbt.unique_id")
        if dbt_model:
            model_name = dbt_model.split(".")[-1]
            models.append(model_name)
            continue

        if name:
            models.append(name.split(".")[-1])
            continue

        if "dbt" in urn.lower():
            segment = urn.rsplit(",", 1)[-1].rstrip(")")
            models.append(segment.split(".")[-1])

    return list(dict.fromkeys(models))


def get_downstream_dbt_models(table_name: str) -> list[str]:
    """
    Query DataHub GraphQL for downstream dbt models affected by a table change.

    Falls back to a mocked response when DataHub is unavailable so demo flows
    continue working regardless of DataHub uptime.
    """
    if not DATAHUB_GMS_URL:
        logger.warning("DATAHUB_GMS_URL not set; returning mocked downstream models")
        return MOCK_DOWNSTREAM_MODELS.copy()

    urn = _build_dataset_urn(table_name)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if DATAHUB_TOKEN:
        headers["Authorization"] = f"Bearer {DATAHUB_TOKEN}"

    payload = {
        "query": DATAHUB_LINEAGE_QUERY,
        "variables": {"urn": urn},
    }

    try:
        response = requests.post(
            DATAHUB_GMS_URL,
            headers=headers,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()

        if body.get("errors"):
            logger.error("DataHub GraphQL errors: %s", body["errors"])
            return MOCK_DOWNSTREAM_MODELS.copy()

        models = _extract_dbt_model_names(body)
        if not models:
            logger.warning(
                "No downstream dbt models found for %s; using mock fallback",
                table_name,
            )
            return MOCK_DOWNSTREAM_MODELS.copy()

        logger.info("DataHub downstream models for %s: %s", table_name, models)
        return models

    except requests.RequestException as exc:
        logger.error("DataHub request failed: %s", exc)
        return MOCK_DOWNSTREAM_MODELS.copy()
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Failed to parse DataHub response: %s", exc)
        return MOCK_DOWNSTREAM_MODELS.copy()


# ---------------------------------------------------------------------------
# C. AI Code Generator
# ---------------------------------------------------------------------------

DBT_FIX_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are an expert dbt and SQL engineer. Given an upstream schema "
            "migration diff and the current dbt model files, produce updated SQL "
            "and schema YAML that keep the model valid and aligned with the new "
            "upstream schema. Preserve model intent, fix column references, and "
            "update YAML column metadata to match.",
        ),
        (
            "human",
            "Upstream schema diff:\n```\n{diff_text}\n```\n\n"
            "dbt model name: {model_name}\n\n"
            "Current SQL ({sql_path}):\n```sql\n{current_sql}\n```\n\n"
            "Current schema YAML ({yaml_path}):\n```yaml\n{current_yaml}\n```\n\n"
            "Return the full corrected SQL and YAML.",
        ),
    ]
)


def generate_dbt_fix(
    diff_text: str,
    model_name: str,
    current_sql: str,
    current_yaml: str,
    sql_path: str = "models/stg_users.sql",
    yaml_path: str = "models/schema.yml",
) -> DBTModelUpdate:
    """Use Gemini with structured output to generate fixed dbt SQL and YAML."""
    from langchain_google_genai import ChatGoogleGenAI
    import os
    
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    llm = ChatGoogleGenAI(model="gemini-1.5-flash", temperature=0, google_api_key=gemini_key)
    structured_llm = llm.with_structured_output(DBTModelUpdate)

    chain = DBT_FIX_PROMPT | structured_llm
    result: DBTModelUpdate = chain.invoke(
        {
            "diff_text": diff_text,
            "model_name": model_name,
            "current_sql": current_sql,
            "current_yaml": current_yaml,
            "sql_path": sql_path,
            "yaml_path": yaml_path,
        }
    )

    try:
        yaml.safe_load(result.updated_yaml)
        logger.info("YAML validation passed for model %s", model_name)
    except yaml.YAMLError as exc:
        logger.error(
            "YAML validation failed for model %s: %s",
            model_name,
            exc,
        )

    logger.info("Generated fix for %s: %s", model_name, result.reasoning[:200])
    return result


# ---------------------------------------------------------------------------
# Helpers — diff parsing & GitHub file fetch
# ---------------------------------------------------------------------------


def extract_table_name_from_diff(diff_text: str) -> str:
    """Best-effort extraction of the affected table name from a SQL diff."""
    patterns = [
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`\"']?([\w.]+)[`\"']?",
        r"RENAME\s+COLUMN\s+[`\"']?[\w]+[`\"']?\s+TO\s+[`\"']?[\w]+[`\"']?",
        r"ON\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`\"']?([\w.]+)[`\"']?",
    ]
    for pattern in patterns[:1]:
        match = re.search(pattern, diff_text, re.IGNORECASE)
        if match:
            table = match.group(1)
            return table.split(".")[-1]

    for line in diff_text.splitlines():
        if re.search(r"alter\s+table|rename\s+column", line, re.IGNORECASE):
            tokens = re.findall(r"[\w.]+", line)
            for token in reversed(tokens):
                if token.lower() not in {"alter", "table", "column", "rename", "to", "if", "exists"}:
                    return token.split(".")[-1]

    return "users"


def fetch_github_file(repo_full_name: str, file_path: str, ref: str = "main") -> str:
    """Fetch a file's decoded content from a GitHub repository."""
    gh = Github(GITHUB_TOKEN)
    repo = gh.get_repo(repo_full_name)

    refs_to_try = [ref, "main", "master"]
    last_error: Exception | None = None

    for branch_ref in refs_to_try:
        try:
            content = repo.get_contents(file_path, ref=branch_ref)
            if isinstance(content, list):
                raise FileNotFoundError(f"{file_path} is a directory, not a file")
            return content.decoded_content.decode("utf-8")
        except GithubException as exc:
            last_error = exc
            continue

    raise FileNotFoundError(
        f"Could not fetch {file_path} from {repo_full_name}: {last_error}"
    )


def get_default_branch(repo_full_name: str) -> str:
    """Return the default branch name for a repository."""
    gh = Github(GITHUB_TOKEN)
    repo = gh.get_repo(repo_full_name)
    return repo.default_branch


# ---------------------------------------------------------------------------
# D. GitHub PR Creator
# ---------------------------------------------------------------------------


def create_fix_pr(repo_full_name: str, pr_number: int, diff_text: str) -> None:
    """
    Detect downstream dbt models, generate fixes via LLM, and open a fix PR
    in the data engineering repository.
    """
    if not GITHUB_TOKEN:
        logger.error("GITHUB_TOKEN is not configured; cannot create fix PR")
        return

    if not DATA_REPO_NAME:
        logger.error("DATA_REPO_NAME is not configured; cannot create fix PR")
        return

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not configured; cannot generate fix")
        return

    table_name = extract_table_name_from_diff(diff_text)
    downstream_models = get_downstream_dbt_models(table_name)
    model_name = downstream_models[0]

    sql_path = f"models/{model_name}.sql"
    yaml_path = "models/schema.yml"

    logger.info(
        "Creating fix PR for downstream model %s (table=%s, source_pr=#%s)",
        model_name,
        table_name,
        pr_number,
    )

    try:
        default_branch = get_default_branch(DATA_REPO_NAME)
        current_sql = fetch_github_file(DATA_REPO_NAME, sql_path, ref=default_branch)
        current_yaml = fetch_github_file(DATA_REPO_NAME, yaml_path, ref=default_branch)
    except FileNotFoundError as exc:
        logger.error("Failed to fetch dbt files: %s", exc)
        current_sql = f"-- dbt model: {model_name}\nSELECT * FROM source_table\n"
        current_yaml = (
            f"version: 2\n\nmodels:\n  - name: {model_name}\n"
            "    description: Auto-generated placeholder schema entry\n"
            "    columns:\n      - name: id\n        description: Primary key\n"
        )
        logger.warning("Using placeholder SQL/YAML for demo continuity")

    fix = generate_dbt_fix(
        diff_text=diff_text,
        model_name=model_name,
        current_sql=current_sql,
        current_yaml=current_yaml,
        sql_path=sql_path,
        yaml_path=yaml_path,
    )

    gh = Github(GITHUB_TOKEN)
    data_repo = gh.get_repo(DATA_REPO_NAME)
    branch_name = f"bot/schema-fix-pr-{pr_number}"

    source_ref = data_repo.get_git_ref(f"heads/{default_branch}")
    base_sha = source_ref.object.sha

    try:
        data_repo.get_git_ref(f"heads/{branch_name}")
        logger.info("Branch %s already exists; updating commits", branch_name)
        data_repo.get_git_ref(f"heads/{branch_name}").delete()
    except GithubException:
        pass

    data_repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)

    base_commit = data_repo.get_git_commit(base_sha)
    base_tree = data_repo.get_git_tree(base_sha, recursive=True)
    existing_paths = {item.path for item in base_tree.tree}

    tree_elements: list[InputGitTreeElement] = []

    for path, content in [(sql_path, fix.updated_sql), (yaml_path, fix.updated_yaml)]:
        if path in existing_paths:
            blob = data_repo.create_git_blob(content, "utf-8")
            tree_elements.append(
                InputGitTreeElement(
                    path=path,
                    mode="100644",
                    type="blob",
                    sha=blob.sha,
                )
            )
        else:
            data_repo.create_file(
                path=path,
                message=f"SchemaGuard: add {path} for {model_name}",
                content=content,
                branch=branch_name,
            )

    if tree_elements:
        new_tree = data_repo.create_git_tree(tree_elements, base_tree)
        commit_message = (
            f"SchemaGuard: fix {model_name} for upstream schema change (PR #{pr_number})"
        )
        new_commit = data_repo.create_git_commit(
            message=commit_message,
            tree=new_tree,
            parents=[base_commit],
        )
        ref = data_repo.get_git_ref(f"heads/{branch_name}")
        ref.edit(sha=new_commit.sha)

    pr_title = f"[SchemaGuard] Fix {model_name} after upstream schema change (PR #{pr_number})"
    pr_body = (
        f"## SchemaGuard Auto-Fix\n\n"
        f"This PR was automatically generated by SchemaGuard in response to "
        f"upstream schema changes detected in [{repo_full_name}#{pr_number}]"
        f"(https://github.com/{repo_full_name}/pull/{pr_number}).\n\n"
        f"### Affected table\n`{table_name}`\n\n"
        f"### Downstream dbt model\n`{model_name}`\n\n"
        f"### LLM reasoning\n{fix.reasoning}\n\n"
        f"### Detected downstream models (DataHub)\n"
        f"```json\n{json.dumps(downstream_models, indent=2)}\n```\n\n"
        f"---\n*Generated by SchemaGuard*"
    )

    existing_prs = data_repo.get_pulls(state="open", head=f"{data_repo.owner.login}:{branch_name}")
    if existing_prs.totalCount > 0:
        logger.info("Fix PR already open: %s", existing_prs[0].html_url)
        return

    fix_pr = data_repo.create_pull(
        title=pr_title,
        body=pr_body,
        head=branch_name,
        base=default_branch,
    )
    logger.info("Opened fix PR: %s", fix_pr.html_url)


# ---------------------------------------------------------------------------
# E. FastAPI Webhook Handler
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SchemaGuard",
    description="AI agent for auto-fixing downstream dbt models after schema changes",
    version="1.0.0",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "schemaguard"}


@app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """
    Handle GitHub pull_request webhook events.

    On opened/synchronize events containing schema DDL changes, enqueue a
    background task to generate and open a dbt fix PR.
    """
    try:
        payload: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        logger.warning("Received webhook with invalid JSON body")
        return {"status": "ignored", "reason": "invalid_json"}

    action = payload.get("action", "")
    pull_request = payload.get("pull_request")

    if action not in {"opened", "synchronize"} or not pull_request:
        return {"status": "ignored", "reason": f"action={action}"}

    diff_url = pull_request.get("diff_url")
    if not diff_url:
        logger.warning("Pull request payload missing diff_url")
        return {"status": "ignored", "reason": "missing_diff_url"}

    headers = {"Accept": "application/vnd.github.v3.diff"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        diff_response = requests.get(diff_url, headers=headers, timeout=30)
        diff_response.raise_for_status()
        diff_text = diff_response.text
    except requests.RequestException as exc:
        logger.error("Failed to fetch PR diff from %s: %s", diff_url, exc)
        return {"status": "error", "reason": "diff_fetch_failed"}

    if not SCHEMA_CHANGE_PATTERN.search(diff_text):
        logger.info("PR #%s has no schema DDL changes; skipping", pull_request.get("number"))
        return {"status": "ignored", "reason": "no_schema_change"}

    repository = payload.get("repository", {})
    repo_full_name = repository.get("full_name", "")
    pr_number = pull_request.get("number", 0)

    if not repo_full_name or not pr_number:
        return {"status": "ignored", "reason": "missing_repo_or_pr_number"}

    logger.info(
        "Schema change detected in %s PR #%s; enqueueing fix",
        repo_full_name,
        pr_number,
    )

    background_tasks.add_task(create_fix_pr, repo_full_name, pr_number, diff_text)

    return {
        "status": "accepted",
        "repo": repo_full_name,
        "pr_number": pr_number,
        "message": "Schema change detected; fix PR generation enqueued",
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
