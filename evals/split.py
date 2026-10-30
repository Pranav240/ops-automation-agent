"""Stratified dev / held-out test split. Run once; the test file is then frozen.

Usage: python -m evals.split          (refuses to overwrite an existing test split)
"""

import hashlib
import json
import random
import sys
from datetime import UTC, datetime

from evals.validate_dataset import DATASET, load_rows, validate

DATASET_DIR = DATASET.parent
DEV = DATASET_DIR / "dev.jsonl"
TEST = DATASET_DIR / "test.jsonl"
TEST_HASH = DATASET_DIR / "test.sha256"
SPLIT_RECORD = DATASET_DIR / "split.json"
SEED = 42
TEST_PER_CATEGORY = 10


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    rng = random.Random(SEED)
    dev, test = [], []
    for category in sorted({r["category"] for r in rows}):
        group = sorted((r for r in rows if r["category"] == category), key=lambda r: r["id"])
        rng.shuffle(group)
        test += group[:TEST_PER_CATEGORY]
        dev += group[TEST_PER_CATEGORY:]
    return sorted(dev, key=lambda r: r["id"]), sorted(test, key=lambda r: r["id"])


def main():
    if TEST.exists():
        sys.exit(f"{TEST} already exists. The test split is frozen; not overwriting.")
    problems = validate()
    if problems:
        sys.exit(f"dataset has {len(problems)} problems; run python -m evals.validate_dataset")

    dev, test = split(load_rows(DATASET))
    write_jsonl(DEV, dev)
    write_jsonl(TEST, test)
    TEST_HASH.write_text(f"{sha256(TEST)}  test.jsonl\n", encoding="utf-8", newline="\n")
    record = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": SEED,
        "test_per_category": TEST_PER_CATEGORY,
        "source_sha256": sha256(DATASET),
        "dev_rows": len(dev),
        "test_rows": len(test),
        "dev_ids": [r["id"] for r in dev],
        "test_ids": [r["id"] for r in test],
    }
    SPLIT_RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"dev={len(dev)} test={len(test)} test_sha256={sha256(TEST)}")


if __name__ == "__main__":
    main()
