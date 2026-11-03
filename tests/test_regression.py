"""Regression suite: every dev failure found during Phase 3 becomes a case here.

Live cases call the real model (Haiku) and run only with RUN_LIVE=1 (the CI eval gate sets it).
Cases are listed in evals/regression_cases.json with the failure that was seen.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from evals.metrics import is_correct, is_wrong

CASES = json.loads((Path(__file__).parent.parent / "evals" / "regression_cases.json").read_text())

live = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="live LLM test; set RUN_LIVE=1")


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
@pytest.mark.parametrize("case", CASES, ids=[c["request_id"] for c in CASES])
def test_regression_case(case, agent):
    from evals.runner import run_agent

    rows, llm, retriever = agent
    row = rows[case["request_id"]]
    rec = asyncio.run(run_agent(row, llm=llm, retriever=retriever, critic=True))
    assert rec["status"] != "error", rec["error"]
    assert not is_wrong(rec, row), f"wrong action {rec['action']} {rec['args']}"
    assert is_correct(rec, row), f"was: {case['seen']}; now: {rec['action'] or rec['kind']}"


def test_cases_are_dev_rows_with_a_note():
    dev_ids = {
        json.loads(line)["id"]
        for line in (Path(__file__).parent.parent / "evals" / "dataset" / "dev.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    for case in CASES:
        assert case["request_id"] in dev_ids
        assert case["seen"]
