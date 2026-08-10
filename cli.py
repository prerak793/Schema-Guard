"""
SchemaGuard CLI - Runs the agent logic in a GitHub Action environment.
"""

import json
import logging
import os
import sys

import requests

from main import SCHEMA_CHANGE_PATTERN, GITHUB_TOKEN, create_fix_pr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("schemaguard.cli")

def run():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        logger.error("GITHUB_EVENT_PATH not found or invalid. Are you running this in a GitHub Action?")
        sys.exit(1)

    with open(event_path, "r") as f:
        payload = json.load(f)

    action = payload.get("action", "")
    pull_request = payload.get("pull_request")

    if action not in {"opened", "synchronize"} or not pull_request:
        logger.info(f"Ignoring event: action={action}, pull_request_present={bool(pull_request)}")
        sys.exit(0)

    diff_url = pull_request.get("diff_url")
    if not diff_url:
        logger.warning("Pull request payload missing diff_url")
        sys.exit(0)

    headers = {"Accept": "application/vnd.github.v3.diff"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    logger.info(f"Fetching diff from {diff_url}")
    try:
        diff_response = requests.get(diff_url, headers=headers, timeout=30)
        diff_response.raise_for_status()
        diff_text = diff_response.text
    except requests.RequestException as exc:
        logger.error(f"Failed to fetch PR diff from {diff_url}: {exc}")
        sys.exit(1)

    if not SCHEMA_CHANGE_PATTERN.search(diff_text):
        logger.info(f"PR #{pull_request.get('number')} has no schema DDL changes; skipping.")
        sys.exit(0)

    repository = payload.get("repository", {})
    repo_full_name = repository.get("full_name", "")
    pr_number = pull_request.get("number", 0)

    if not repo_full_name or not pr_number:
        logger.error("Missing repository full_name or pull_request number in payload")
        sys.exit(1)

    logger.info(f"Schema change detected in {repo_full_name} PR #{pr_number}; generating fix...")
    create_fix_pr(repo_full_name, pr_number, diff_text)
    logger.info("Finished processing PR.")

if __name__ == "__main__":
    run()
