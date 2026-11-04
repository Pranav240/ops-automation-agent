"""Send a burst through API Gateway and record the workers scaling up and back down.

Requests are order status questions built from the mock API's seed orders, so neither evaluation
split is touched. Each one costs about $0.002 in LLM calls.

Needs the AWS stack, a cluster running the chart, and .env with OPS_API_URL.

Usage: uv run python scripts/scale_test.py --count 40 --cluster kind
"""

import argparse
import json
import os
import subprocess
import time
from datetime import date
from pathlib import Path

import boto3
from dotenv import load_dotenv

from agent.config import git_state
from integrations.submit import signed_post

SEED = Path("mock_api/seed_orders.json")
SEND_GAP_S = 0.6  # the API allows 2 requests a second
SAMPLE_S = 5
TIMEOUT_S = 1200


def build_requests(count: int, prefix: str) -> list[dict]:
    orders = json.loads(SEED.read_text(encoding="utf-8"))["orders"]
    requests = []
    for i in range(count):
        order = orders[i % len(orders)]
        requests.append(
            {
                "id": f"{prefix}-{i:03d}",
                "customer_email": order["customer_email"],
                "request_text": f"What is the current status of my order {order['order_id']}?",
            }
        )
    return requests


def summarize(samples: list[dict]) -> dict:
    """What the samples show. `replicas` is what KEDA asked for, `ready` is what was serving."""
    peak = max(s["replicas"] for s in samples)
    first_up = next((s["t_s"] for s in samples if s["replicas"] > 0), None)
    first_peak = next((s["t_s"] for s in samples if s["replicas"] == peak), None)
    last_busy = max((s["t_s"] for s in samples if s["replicas"] > 0), default=None)
    return {
        "peak_replicas": peak,
        "peak_ready": max(s["ready"] for s in samples),
        "peak_queue": max(s["visible"] + s["in_flight"] for s in samples),
        "first_scale_up_s": first_up,
        "reached_peak_s": first_peak,
        "last_worker_seen_s": last_busy,
        "back_to_zero": samples[-1]["replicas"] == 0,
    }


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=True).stdout


def workers() -> tuple[int, int]:
    status = json.loads(kubectl("get", "deploy", "ops-agent-worker", "-o", "json"))
    return status["spec"].get("replicas") or 0, status["status"].get("readyReplicas") or 0


def queue_depth(sqs, url: str) -> tuple[int, int]:
    names = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=names)["Attributes"]
    return int(attributes[names[0]]), int(attributes[names[1]])


def run_rows(prefix: str) -> list[dict]:
    query = (
        "select coalesce(json_agg(r), '[]') from (select request_id, status, kind, cost_usd, "
        f"latency_s from agent_runs where request_id like '{prefix}-%') r"
    )
    command = f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "{query}"'
    return json.loads(kubectl("exec", "deploy/ops-agent-postgres", "--", "sh", "-c", command))


def main(args) -> None:
    load_dotenv(".env")
    prefix = f"SCALE-{int(time.time())}"
    session = boto3.Session()
    sqs = session.client("sqs")
    url = f"{os.environ['OPS_API_URL'].rstrip('/')}/requests"
    queue_url = os.environ["OPS_WORK_QUEUE_URL"]

    began = time.monotonic()
    pending = build_requests(args.count, prefix)
    samples, last_sample, last_send = [], -SAMPLE_S, 0.0
    while time.monotonic() - began < TIMEOUT_S:
        now = time.monotonic()
        if pending and now - last_send >= SEND_GAP_S:
            response = signed_post(url, json.dumps(pending[0]), session)
            if response.status_code == 200:
                pending.pop(0)
            last_send = now
        if now - last_sample >= SAMPLE_S:
            last_sample = now
            replicas, ready = workers()
            visible, in_flight = queue_depth(sqs, queue_url)
            samples.append(
                {
                    "t_s": round(now - began, 1),
                    "sent": args.count - len(pending),
                    "replicas": replicas,
                    "ready": ready,
                    "visible": visible,
                    "in_flight": in_flight,
                }
            )
            print(samples[-1])
            scaled_up = any(s["replicas"] > 0 for s in samples)
            if not pending and scaled_up and replicas == 0 and visible + in_flight == 0:
                break
        time.sleep(0.1)

    rows = run_rows(prefix)
    statuses = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    result = {
        "date": str(date.today()),
        **git_state(),
        "cluster": args.cluster,
        "model": os.getenv("LLM_MODEL", "claude-haiku-5-5"),
        "requests": args.count,
        "runs_recorded": len(rows),
        "statuses": statuses,
        "llm_cost_usd": round(sum(row["cost_usd"] for row in rows), 6),
        **summarize(samples),
        "samples": samples,
    }
    path = Path(f"results/{date.today()}_{args.cluster}_scaling.json")
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print({k: v for k, v in result.items() if k != "samples"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--cluster", default="kind")
    main(parser.parse_args())
