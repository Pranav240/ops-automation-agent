"""Write deploy/helm/values.local.yaml (gitignored) for the workers on kind, from .env.

AWS access uses temporary keys for the worker role, so the cluster never holds a long-lived key.
They last 1 hour: run this again and `helm upgrade` when they expire.

Usage: uv run python scripts/kind_values.py
"""

import json
import os
import subprocess
from pathlib import Path

import boto3
from dotenv import load_dotenv

OUT = Path("deploy/helm/values.local.yaml")
FROM_ENV = [
    "POSTGRES_PASSWORD",
    "SQL_READER_PASSWORD",
    "ANTHROPIC_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "JIRA_API_TOKEN",
]
CONFIG = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_PROJECT_KEY", "OPS_WORK_QUEUE_URL", "OPS_RUNS_TABLE"]


def terraform_output(name: str) -> str:
    command = ["terraform", "-chdir=infra/aws", "output", "-raw", name]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def main() -> None:
    load_dotenv(".env")
    role = boto3.client("sts").assume_role(
        RoleArn=terraform_output("worker_role_arn"), RoleSessionName="kind-worker"
    )["Credentials"]
    secrets = {name: os.getenv(name, "") for name in FROM_ENV}
    secrets["AWS_ACCESS_KEY_ID"] = role["AccessKeyId"]
    secrets["AWS_SECRET_ACCESS_KEY"] = role["SecretAccessKey"]
    secrets["AWS_SESSION_TOKEN"] = role["SessionToken"]
    config = {name: os.getenv(name, "") for name in CONFIG}
    config["OPS_WORK_QUEUE_URL"] = terraform_output("work_queue_url")
    # JSON is valid YAML, and it quotes every value safely.
    OUT.write_text(json.dumps({"secrets": secrets, "config": config}, indent=2), encoding="utf-8")
    print(f"wrote {OUT}; keys expire at {role['Expiration']}")


if __name__ == "__main__":
    main()
