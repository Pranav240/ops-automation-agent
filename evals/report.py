"""Turn runner output into a results table (markdown + json).

Runs of the same system are grouped; rates are shown as the mean over repeats (with the
standard deviation when there are several) and a 95% Wilson interval from the first run.

Usage:
  python -m evals.report results/runs/2026-11-03_dev_*.jsonl --out results/2026-11-03_dev
  python -m evals.report FILES... --compare A-H@abc B1@abc   # paired bootstrap; repeatable
"""

import argparse
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from evals.metrics import is_correct, is_wrong, summarize

DATASET = Path(__file__).parent / "dataset"

RATE_ROWS = [
    ("correct", "Correct action rate"),
    ("wrong", "Wrong action rate"),
    ("escalation_recall", "Escalation recall"),
    ("escalation_precision", "Escalation precision"),
    ("escalation_reason_accuracy", "Escalation reason accuracy"),
    ("injection_resistance", "Injection resistance"),
    ("groundedness", "Groundedness (cited in retrieved)"),
    ("no_citation_share", "Replies with no citation"),
    ("retrieval_hit_at_5", "Retrieval hit@5 (agent query)"),
]


def load_runs(paths: list[Path]) -> dict[str, list[list[dict]]]:
    """'system@commit' -> list of runs (each a list of records). Repeats share a commit."""
    runs: dict[str, list[list[dict]]] = defaultdict(list)
    for path in sorted(paths):
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        runs[f"{records[0]['system']}@{records[0]['commit']}"].append(records)
    return runs


def load_rows(split: str) -> dict[str, dict]:
    lines = (DATASET / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
    return {r["id"]: r for r in map(json.loads, lines)}


def paired_bootstrap(a: list[bool], b: list[bool], resamples: int = 10_000, seed: int = 0):
    """Difference in rate (a - b) over the same requests, with a 95% bootstrap interval."""
    rng = random.Random(seed)
    n = len(a)
    diffs = []
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        diffs.append(sum(a[i] - b[i] for i in idx) / n)
    diffs.sort()
    return (sum(a) - sum(b)) / n, (diffs[int(0.025 * resamples)], diffs[int(0.975 * resamples)])


def fmt_rate(summaries: list[dict], key: str) -> str:
    values = [s[key] for s in summaries if key in s and s[key]["n"]]
    if not values:
        return "-"
    mean = statistics.mean(v["rate"] for v in values)
    first = values[0]
    text = f"{mean:.2f}"
    if len(values) > 1:
        text += f" ± {statistics.stdev(v['rate'] for v in values):.2f}"
    low, high = first["ci95"]
    return f"{text} [{low:.2f}, {high:.2f}] ({first['k']}/{first['n']})"


def fmt_value(summaries: list[dict], key: str, digits: int) -> str:
    values = [s[key] for s in summaries if s.get(key) is not None]
    if not values:
        return "-"
    return f"{statistics.mean(values):.{digits}f}"


def build(runs: dict[str, list[list[dict]]], rows: dict[str, dict]) -> tuple[dict, str]:
    summaries = {label: [summarize(run, rows) for run in group] for label, group in runs.items()}
    labels = list(summaries)
    first = {label: runs[label][0][0] for label in labels}

    lines = ["| Metric | " + " | ".join(labels) + " |", "|---" * (len(labels) + 1) + "|"]

    def add(name, cells):
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    add("Model", [str(first[label]["model"]) for label in labels])
    add("Commit", [first[label]["commit"] for label in labels])
    add("Runs x requests", [f"{len(summaries[lb])} x {summaries[lb][0]['n']}" for lb in labels])
    add("Errors (first run)", [str(summaries[lb][0]["errors"]) for lb in labels])
    for key, name in RATE_ROWS:
        add(name, [fmt_rate(summaries[lb], key) for lb in labels])
    add(
        "Retrieval MRR@5 (agent query)",
        [fmt_value(summaries[lb], "retrieval_mrr_at_5", 2) for lb in labels],
    )
    add(
        "Owner-mismatch actions",
        [fmt_value(summaries[lb], "owner_mismatch_actions", 0) for lb in labels],
    )
    add(
        "Cost per request (USD)",
        [fmt_value(summaries[lb], "cost_per_request_usd", 5) for lb in labels],
    )
    add(
        "Latency p50 / p95 (s)",
        [
            fmt_value(summaries[lb], "latency_p50_s", 1)
            + " / "
            + fmt_value(summaries[lb], "latency_p95_s", 1)
            for lb in labels
        ],
    )

    categories = sorted({c for s in summaries.values() for c in s[0]["by_category"]})
    lines += ["", "Correct / wrong action rate by category (first run):", ""]
    lines += ["| Category | " + " | ".join(labels) + " |", "|---" * (len(labels) + 1) + "|"]
    for category in categories:
        cells = []
        for label in labels:
            c = summaries[label][0]["by_category"].get(category)
            cells.append(f"{c['correct']:.2f} / {c['wrong']:.2f} (n={c['n']})" if c else "-")
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    return summaries, "\n".join(lines) + "\n"


def compare(runs, rows, a: str, b: str) -> str:
    first_a = {r["request_id"]: r for r in runs[a][0]}
    first_b = {r["request_id"]: r for r in runs[b][0]}
    ids = sorted(set(first_a) & set(first_b))
    lines = []
    for name, fn in (("correct", is_correct), ("wrong", is_wrong)):
        flags_a = [fn(first_a[i], rows[i]) for i in ids]
        flags_b = [fn(first_b[i], rows[i]) for i in ids]
        diff, (low, high) = paired_bootstrap(flags_a, flags_b)
        verdict = "significant" if low > 0 or high < 0 else "not distinguishable"
        interval = f"[{low:+.3f}, {high:+.3f}]"
        lines.append(f"{a} - {b}, {name} rate: {diff:+.3f} {interval} (n={len(ids)}, {verdict})")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, help="write OUT.md and OUT.json")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), action="append", default=[])
    args = parser.parse_args(argv)

    runs = load_runs(args.files)
    splits = {run[0]["split"] for group in runs.values() for run in group}
    if len(splits) != 1:
        raise SystemExit(f"mixed splits: {splits}")
    rows = load_rows(splits.pop())
    summaries, table = build(runs, rows)
    for a, b in args.compare:
        table += "\n" + compare(runs, rows, a, b)
    print(table)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".md").write_text(table, encoding="utf-8")
        payload = {"files": [str(p) for p in args.files], "summaries": summaries}
        text = json.dumps(payload, indent=1) + "\n"
        args.out.with_suffix(".json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
