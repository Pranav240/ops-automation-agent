"""CI gate: run the agent on a fixed dev subset and fail if it regresses.

The subset and thresholds live in evals/ci_gate.json. Exit code 1 means the gate failed.

Usage: python -m evals.ci_gate
"""

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.config import db_url
from agent.llm import LLMClient
from evals.metrics import summarize
from evals.runner import load_split, run_system
from retrieval.search import HybridRetriever

CONFIG = Path(__file__).parent / "ci_gate.json"


def check(summary: dict, limits: dict) -> list[str]:
    failures = []
    if summary["errors"] > limits["max_errors"]:
        failures.append(f"errors {summary['errors']} > {limits['max_errors']}")
    if summary["correct"]["rate"] < limits["min_correct"]:
        failures.append(f"correct {summary['correct']['rate']:.2f} < {limits['min_correct']}")
    if summary["wrong"]["rate"] > limits["max_wrong"]:
        failures.append(f"wrong {summary['wrong']['rate']:.2f} > {limits['max_wrong']}")
    injection = summary["injection_resistance"]
    if injection["n"] and injection["rate"] < limits["min_injection_resistance"]:
        failures.append(f"injection resistance {injection['k']}/{injection['n']}")
    if summary["owner_mismatch_actions"] > 0:
        failures.append(f"{summary['owner_mismatch_actions']} actions on other customers' orders")
    return failures


async def main() -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    wanted = set(config["request_ids"])
    rows = [r for r in load_split("dev") if r["id"] in wanted]
    llm = LLMClient.from_env()
    records = await run_system(
        "A-H", rows, llm, HybridRetriever(db_url()), critic=True, concurrency=4, timeout_s=180
    )
    llm.tracer.flush()
    summary = summarize(records, {r["id"]: r for r in rows})
    cost = sum(r["cost_usd"] for r in records)
    print(
        f"n={summary['n']} correct={summary['correct']['rate']:.2f} "
        f"wrong={summary['wrong']['rate']:.2f} errors={summary['errors']} cost=${cost:.4f}"
    )
    failures = check(summary, config["thresholds"])
    for failure in failures:
        print(f"FAIL: {failure}")
    print("gate passed" if not failures else "gate failed")
    return 1 if failures else 0


if __name__ == "__main__":
    load_dotenv(".env")
    sys.exit(asyncio.run(main()))
