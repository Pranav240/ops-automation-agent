"""Run systems over a dataset split and save one JSON line per (system, repeat, request).

Systems: B1 (single LLM call), B2 (rule router), A-H (full agent). Agent options:
--no-critic and --retrieval MODE (ablations). Each request gets a fresh in-memory mock API.
An agent run that pauses for approval is recorded as sent to approval and not resumed.

The test split is refused unless --allow-test is given, and its hash must match the frozen one
(test runs belong to Phase 8 only).

Usage:
  python -m evals.runner --systems B2,B1,A-H --split dev
  python -m evals.runner --systems A-H --no-critic --ids REQ-002 REQ-004
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver

from agent.baselines import order_view, rule_router, single_call
from agent.config import db_url, git_state
from agent.graph import build_graph, config, start
from agent.llm import LLMClient
from agent.nodes import Deps
from agent.tools import WRITE_TOOLS, MockApi
from mock_api.app import create_app
from retrieval.search import MODES, HybridRetriever

ROOT = Path(__file__).parent.parent
DATASET = ROOT / "evals" / "dataset"
RUNS_DIR = ROOT / "results" / "runs"
WRITE_ACTIONS = {action for action, _ in WRITE_TOOLS.values()}
SYSTEMS = ("B1", "B2", "A-H")


def load_split(split: str, allow_test: bool = False) -> list[dict]:
    path = DATASET / f"{split}.jsonl"
    if split == "test":
        if not allow_test:
            sys.exit("the test split is frozen for Phase 8; pass --allow-test only then")
        expected = (DATASET / "test.sha256").read_text().split()[0]
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            sys.exit("test.jsonl does not match test.sha256")
    elif split != "dev":
        sys.exit(f"unknown split {split}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fresh_api() -> MockApi:
    return MockApi("http://mock", transport=httpx.ASGITransport(app=create_app()))


def base_record(row: dict) -> dict:
    return {
        "request_id": row["id"],
        "category": row["category"],
        "action": None,
        "args": {},
        "label": None,
        "kind": None,
        "reply": "",
        "cited_policies": [],
        "retrieved": [],
        "looked_up": [],
        "sent_to_approval": False,
        "executed": False,
        "status": None,
        "error": None,
        "cost_usd": 0.0,
        "llm_calls": 0,
    }


def baseline_record(rec: dict, decision: dict) -> dict:
    """Baselines have no approval gate: a write decision counts as executed."""
    action = decision["action"]
    if action in WRITE_ACTIONS:
        rec.update(action=action, executed=True)
    else:
        rec["label"] = action
    rec.update(args=decision["args"], reply=decision["reply"], status="done")
    return rec


async def run_b1(row, llm, **_):
    rec = base_record(row)
    run = llm.start_run(uuid.uuid4().hex)
    api = fresh_api()
    try:
        baseline_record(rec, await single_call(run, api, row))
    except Exception as e:
        rec.update(status="error", error=f"{type(e).__name__}: {e}")
    finally:
        rec.update(cost_usd=run.budget.spent_usd, llm_calls=run.budget.calls)
        await api.aclose()
    return rec


async def run_b2(row, **_):
    rec = base_record(row)
    api = fresh_api()
    try:
        order = await order_view(api, row["request_text"], row["customer_email"])
        return baseline_record(rec, rule_router(row, order))
    finally:
        await api.aclose()


async def run_agent(row, llm, retriever, critic, **_):
    rec = base_record(row)
    api = fresh_api()
    deps = Deps(llm=llm, retriever=retriever, api=api)
    graph = build_graph(deps, InMemorySaver(), critic=critic)
    run_id = uuid.uuid4().hex
    rec["run_id"] = run_id
    try:
        await start(graph, row, run_id=run_id)
    except Exception as e:
        rec.update(status="error", error=f"{type(e).__name__}: {e}")
    finally:
        await api.aclose()
        if run_id in deps.runs:
            budget = deps.runs[run_id].budget
            rec.update(cost_usd=budget.spent_usd, llm_calls=budget.calls)
    if rec["error"]:
        return rec
    values = (await graph.aget_state(config(run_id))).values
    proposal, outcome = values.get("proposal", {}), values.get("outcome")
    rec.update(
        action=proposal.get("action"),
        args=outcome["args"] if outcome else proposal.get("args", {}),
        kind=proposal.get("kind"),
        reply=proposal.get("reply", ""),
        cited_policies=proposal.get("cited_policies", []),
        confidence=proposal.get("confidence"),
        plan_query=values.get("plan", {}).get("search_query"),
        retrieved=[d["doc_id"] for d in values.get("docs", [])],
        looked_up=sorted(values.get("orders", {})),
        revisions=values.get("revisions", 0),
        critique=values.get("critique"),
        sent_to_approval=outcome is None,
        executed=bool(outcome and outcome["executed"]),
        status=outcome["status"] if outcome else "pending_approval",
    )
    return rec


RUNNERS = {"B1": run_b1, "B2": run_b2, "A-H": run_agent}


async def run_system(system, rows, llm, retriever, critic, concurrency, timeout_s):
    semaphore = asyncio.Semaphore(concurrency)

    async def one(row):
        async with semaphore:
            began = time.perf_counter()
            try:
                async with asyncio.timeout(timeout_s):
                    rec = await RUNNERS[system](row, llm=llm, retriever=retriever, critic=critic)
            except Exception as e:
                rec = base_record(row)
                rec.update(status="error", error=f"{type(e).__name__}: {e}")
            rec["latency_s"] = round(time.perf_counter() - began, 3)
            shown = rec["action"] or rec["label"] or rec["kind"]
            print(f"{system} {row['id']} {rec['status']} {shown}")
            return rec

    return await asyncio.gather(*(one(row) for row in rows))


def label_for(system: str, critic: bool, retrieval: str) -> str:
    if system != "A-H":
        return system
    parts = [system] + ([] if critic else ["no-critic"])
    return "_".join(parts + ([] if retrieval == "hybrid_rerank" else [retrieval]))


async def main(args) -> list[Path]:
    rows = load_split(args.split, args.allow_test)
    if args.ids:
        wanted = set(args.ids)
        rows = [r for r in rows if r["id"] in wanted]
        if len(rows) != len(wanted):
            sys.exit("some ids are not in this split")
    llm = LLMClient.from_env()
    if args.model:
        llm.model = args.model
    retriever = HybridRetriever(db_url(), mode=args.retrieval)
    git = git_state()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for system in args.systems.split(","):
        if system not in RUNNERS:
            sys.exit(f"unknown system {system}")
        label = label_for(system, not args.no_critic, args.retrieval)
        meta = {
            "system": label,
            "model": None if system == "B2" else llm.model,
            "split": args.split,
            "date": date.today().isoformat(),
            **git,
        }
        for repeat in range(1, args.repeat + 1):
            records = await run_system(
                system, rows, llm, retriever, not args.no_critic, args.concurrency, args.timeout
            )
            path = RUNS_DIR / f"{meta['date']}_{args.split}_{label}_{git['commit']}_r{repeat}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for rec in sorted(records, key=lambda r: r["request_id"]):
                    f.write(json.dumps({**meta, "repeat": repeat, **rec}) + "\n")
            cost = sum(r["cost_usd"] for r in records)
            print(f"wrote {path.relative_to(ROOT)} ({len(records)} rows, ${cost:.4f})")
            paths.append(path)
    llm.tracer.flush()
    return paths


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--systems", default="B2,B1,A-H")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", help="override LLM_MODEL for B1 and the agent")
    parser.add_argument("--no-critic", action="store_true")
    parser.add_argument("--retrieval", default="hybrid_rerank", choices=MODES)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--allow-test", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    load_dotenv(".env")
    asyncio.run(main(parse_args()))
