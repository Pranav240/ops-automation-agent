"""Run the agent on dev-set requests, and resume runs paused for approval.

Needs Postgres (docker compose up -d), ingested policies (python -m retrieval.ingest) and the
mock API (uvicorn mock_api.app:app --env-file .env). Only the dev split can be loaded here; the
frozen test split is never read by this script.

Usage:
  python -m agent.run REQ-002 REQ-004          # reset the mock API, run each request
  python -m agent.run REQ-002 --out results/x.jsonl   # also save one JSON line per request
  python -m agent.run --resume RUN_ID --decision approve --by owner
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.config import db_url, mock_api_url
from agent.graph import build_graph, resume, start
from agent.llm import LLMClient
from agent.llm.tracing import LangfuseTracer
from agent.nodes import Deps
from agent.tools import MockApi
from retrieval.search import HybridRetriever

DEV_PATH = Path(__file__).parent.parent / "evals" / "dataset" / "dev.jsonl"


def load_dev(ids: list[str]) -> list[dict]:
    rows = {r["id"]: r for r in map(json.loads, DEV_PATH.read_text(encoding="utf-8").splitlines())}
    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(f"not in the dev split: {', '.join(missing)}")
    return [rows[i] for i in ids]


def show(out: dict, tracer) -> None:
    keep = (
        "run_id",
        "status",
        "action",
        "args",
        "kind",
        "reply",
        "cited_policies",
        "retrieved",
        "sent_to_approval",
        "executed",
        "api_result",
        "approval_request",
    )
    print(json.dumps({k: out[k] for k in keep if k in out}, indent=1))
    if isinstance(tracer, LangfuseTracer):
        print(f"trace: {tracer.trace_url(out['run_id'])}")


def meta(model: str) -> dict:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True
        ).stdout
    )
    return {"model": model, "date": date.today().isoformat(), "commit": commit, "dirty": dirty}


async def main(args) -> None:
    llm = LLMClient.from_env()
    api = MockApi(mock_api_url())
    deps = Deps(llm=llm, retriever=HybridRetriever(db_url()), api=api)
    run_meta = meta(llm.model)
    async with AsyncPostgresSaver.from_conn_string(db_url()) as saver:
        await saver.setup()
        graph = build_graph(deps, saver)
        if args.resume:
            answer = {"decision": args.decision, "by": args.by}
            if args.args:
                answer["args"] = json.loads(args.args)
            show(await resume(graph, args.resume, answer), llm.tracer)
        for row in load_dev(args.ids):
            admin = {"Authorization": f"Bearer {os.getenv('MOCK_API_ADMIN_TOKEN', '')}"}
            await api.client.post("/reset", headers=admin)
            print(f"\n== {row['id']} ({row['category']}): {row['request_text']}")
            out = await start(graph, row)
            show(out, llm.tracer)
            spent = deps.runs[out["run_id"]].budget
            print(f"llm calls={spent.calls} cost=${spent.spent_usd:.5f}")
            if args.out:
                record = {
                    **run_meta,
                    "request_id": row["id"],
                    "llm_calls": spent.calls,
                    "cost_usd": round(spent.spent_usd, 6),
                    "outcome": out,
                }
                with open(args.out, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
    await api.aclose()
    llm.tracer.flush()


if __name__ == "__main__":
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--resume")
    parser.add_argument("--decision", choices=["approve", "reject", "edit"])
    parser.add_argument("--by")
    parser.add_argument("--args", help="JSON arguments for --decision edit")
    parser.add_argument("--out", help="append one JSON line per request to this file")
    args = parser.parse_args()
    if args.resume and not (args.decision and args.by):
        parser.error("--resume needs --decision and --by")
    # psycopg's async mode does not work with Windows' default event loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(args), loop_factory=loop_factory)
