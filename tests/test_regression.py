"""Regression suite: every dev failure found during Phase 3 becomes a case here.

The model is not deterministic, so one live run judges the cases together: any error or wrong
action fails it, and at least `regression_min_correct` cases (evals/ci_gate.json) must be correct.
That minimum is the lowest score in the saved dev runs at the frozen commit.

The live test calls the real model (Haiku) and runs only with RUN_LIVE=1 (the CI eval gate sets
it). Cases are listed in evals/regression_cases.json with the failure that was seen.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from evals.metrics import is_correct, is_wrong

EVALS = Path(__file__).parent.parent / "evals"
CASES = json.loads((EVALS / "regression_cases.json").read_text())
MIN_CORRECT = json.loads((EVALS / "ci_gate.json").read_text())["thresholds"][
    "regression_min_correct"
]

live = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="live LLM test; set RUN_LIVE=1")


def suite_failures(results: list[tuple[dict, dict]], min_correct: int) -> list[str]:
    """Judge (record, row) pairs from one run of the suite. Empty means the suite passed."""
    failures = []
    correct = 0
    for rec, row in results:
        if rec["status"] == "error":
            failures.append(f"{row['id']}: error {rec['error']}")
        elif is_wrong(rec, row):
            failures.append(f"{row['id']}: wrong action {rec['action']} {rec['args']}")
        elif is_correct(rec, row):
            correct += 1
    if correct < min_correct:
        failures.append(f"{correct}/{len(results)} correct, needs {min_correct}")
    return failures


@pytest.fixture(scope="module")
def agent():
    load_dotenv(".env")
    from agent.config import db_url
    from agent.llm import LLMClient
    from evals.runner import load_split
    from retrieval.search import HybridRetriever

    rows = {r["id"]: r for r in load_split("dev")}
    return rows, LLMClient.from_env(), HybridRetriever(db_url())


@live
@pytest.mark.live
def test_regression_suite(agent):
    from evals.runner import run_agent

    rows, llm, retriever = agent
    results = []
    for case in CASES:
        row = rows[case["request_id"]]
        rec = asyncio.run(run_agent(row, llm=llm, retriever=retriever, critic=True))
        results.append((rec, row))
    failures = suite_failures(results, MIN_CORRECT)
    assert not failures, "\n".join(failures)


def test_cases_are_dev_rows_with_a_note():
    dev_ids = {
        json.loads(line)["id"]
        for line in (EVALS / "dataset" / "dev.jsonl").read_text(encoding="utf-8").splitlines()
    }
    for case in CASES:
        assert case["request_id"] in dev_ids
        assert case["seen"]
    assert 0 < MIN_CORRECT <= len(CASES)


def fake_result(request_id: str, expected="refuse", **rec):
    row = {"id": request_id, "expected_action": expected, "expected_args": {}, "category": "x"}
    record = {"status": "done", "error": None, "action": None, "args": {}, "executed": False}
    return {**record, "kind": "refusal", **rec}, row


def test_suite_passes_with_enough_correct_and_no_wrong_action():
    results = [fake_result("A"), fake_result("B"), fake_result("C", kind="clarification")]
    assert suite_failures(results, 2) == []


def test_suite_fails_below_the_minimum():
    results = [fake_result("A"), fake_result("B", kind="clarification")]
    assert suite_failures(results, 2) == ["1/2 correct, needs 2"]


def test_one_error_fails_the_suite_even_above_the_minimum():
    results = [fake_result("A"), fake_result("B", status="error", error="boom")]
    assert suite_failures(results, 1) == ["B: error boom"]


def test_one_wrong_action_fails_the_suite_even_above_the_minimum():
    refund = {"action": "refund", "args": {"order_id": "ORD-1"}, "executed": True}
    results = [fake_result("A"), fake_result("B", **refund)]
    failures = suite_failures(results, 1)
    assert len(failures) == 1 and failures[0].startswith("B: wrong action refund")
