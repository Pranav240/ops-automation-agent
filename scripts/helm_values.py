"""Write deploy/helm/values.local.yaml (gitignored) for the workers, from .env.

No long-lived AWS key goes into a cluster. On kind this uses temporary keys for the worker and
dashboard roles: they last 1 hour, so run this again and `helm upgrade` when they expire. On EKS
the workers get their roles through IRSA, the images come from ECR, and the dashboard stays off.

Usage: uv run python scripts/helm_values.py --cluster kind
"""

import argparse
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
    "WORKER_DB_PASSWORD",
    "DASHBOARD_DB_PASSWORD",
    "MOCK_API_READ_TOKEN",
    "MOCK_API_WRITE_TOKEN",
    "MOCK_API_ADMIN_TOKEN",
    "DASHBOARD_VIEWER_TOKEN",
    "DASHBOARD_APPROVER_TOKEN",
    "ANTHROPIC_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "JIRA_API_TOKEN",
]
CONFIG = [
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_PROJECT_KEY",
    "LANGFUSE_BASE_URL",  # the keys belong to one Langfuse region
    "OPS_RUNS_TABLE",
]


def terraform_output(name: str, stack: str = "infra/aws") -> str:
    command = ["terraform", f"-chdir={stack}", "output", "-raw", name]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def temporary_keys(role_output: str, prefix: str = "") -> dict:
    role = boto3.client("sts").assume_role(
        RoleArn=terraform_output(role_output), RoleSessionName=f"kind-{role_output}"
    )["Credentials"]
    print(f"{role_output}: keys expire at {role['Expiration']}")
    return {
        f"{prefix}AWS_ACCESS_KEY_ID": role["AccessKeyId"],
        f"{prefix}AWS_SECRET_ACCESS_KEY": role["SecretAccessKey"],
        f"{prefix}AWS_SESSION_TOKEN": role["SessionToken"],
    }


def kind_values() -> dict:
    return {
        "secrets": temporary_keys("worker_role_arn")
        | temporary_keys("dashboard_role_arn", "DASHBOARD_")
    }


def eks_values(commit: str) -> dict:
    registry = terraform_output("image_registry", "infra/aws/eks")
    role = terraform_output("worker_role_arn", "infra/aws/eks")
    return {
        "images": {
            "worker": f"{registry}/ops-agent-worker:{commit}",
            "mockApi": f"{registry}/ops-agent-mock-api:{commit}",
        },
        "serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": role}},
        "keda": {"auth": "irsa"},
        "dashboard": {"enabled": False},
    }


def main(args) -> None:
    load_dotenv(".env")
    values = kind_values() if args.cluster == "kind" else eks_values(args.commit)
    secrets = {name: os.getenv(name, "") for name in FROM_ENV} | values.pop("secrets", {})
    config = {name: os.getenv(name, "") for name in CONFIG}
    config["OPS_WORK_QUEUE_URL"] = terraform_output("work_queue_url")
    values |= {"secrets": secrets, "config": config}
    # JSON is valid YAML, and it quotes every value safely.
    OUT.write_text(json.dumps(values, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", choices=["kind", "eks"], default="kind")
    parser.add_argument("--commit", default="latest", help="image tag on EKS")
    main(parser.parse_args())
