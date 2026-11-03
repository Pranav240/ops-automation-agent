"""Retrieval-only comparison on the dev set: hit rate@5 and MRR@5 against relevant_policies.

The query is the raw request text (no LLM), so this costs nothing and every variant sees the
same input. Variants: vector, fulltext, hybrid (RRF), hybrid_rerank (pgvector), qdrant (vector).

Usage: python -m evals.retrieval_eval --out results/2026-11-03_dev_retrieval
"""

import argparse
import json
import statistics
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from agent.config import db_url, qdrant_url
from evals.metrics import hit_and_rr, rate
from evals.runner import git_state, load_split
from retrieval.qdrant_store import QdrantRetriever
from retrieval.search import MODES, HybridRetriever


def evaluate(retriever, rows: list[dict]) -> dict:
    scored = []
    for row in rows:
        ids = [h.doc_id for h in retriever.search_sync(row["request_text"])]
        scored.append(hit_and_rr(ids, row["relevant_policies"]))
    hits = rate([hit for hit, _ in scored])
    return {"hit_at_5": hits, "mrr_at_5": statistics.mean(rr for _, rr in scored)}


def main(argv=None):
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    rows = [r for r in load_split("dev") if r["relevant_policies"]]
    retrievers = [HybridRetriever(db_url(), mode=m) for m in MODES] + [
        QdrantRetriever(qdrant_url())
    ]
    results = {r.mode: evaluate(r, rows) for r in retrievers}

    meta = {
        "split": "dev",
        "date": date.today().isoformat(),
        "query": "request_text",
        **git_state(),
    }
    lines = [
        f"Retrieval on dev ({len(rows)} requests with relevant policies), query = request text, "
        f"commit {meta['commit']}{' (dirty)' if meta['dirty'] else ''}, {meta['date']}",
        "",
        "| Variant | Hit rate@5 | MRR@5 |",
        "|---|---|---|",
    ]
    for mode, res in results.items():
        h = res["hit_at_5"]
        ci = f"[{h['ci95'][0]:.2f}, {h['ci95'][1]:.2f}]"
        lines.append(
            f"| {mode} | {h['rate']:.2f} {ci} ({h['k']}/{h['n']}) | {res['mrr_at_5']:.2f} |"
        )
    table = "\n".join(lines) + "\n"
    print(table)
    if args.out:
        args.out.with_suffix(".md").write_text(table, encoding="utf-8")
        args.out.with_suffix(".json").write_text(
            json.dumps({**meta, "results": results}, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
