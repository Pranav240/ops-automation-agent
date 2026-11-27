"""ApiStore against the local Postgres (skipped when it is not running). Uses its own rows only."""

import uuid

import psycopg
import pytest
from dotenv import load_dotenv

from api.store import ApiStore, NotPaused


@pytest.fixture
def store():
    load_dotenv(".env")
    from agent.config import db_url
    from agent.run_history import SCHEMA as HISTORY_SCHEMA

    try:
        url = db_url()
        psycopg.connect(url, connect_timeout=3).close()
    except (KeyError, psycopg.OperationalError):
        pytest.skip("Postgres not reachable")
    with psycopg.connect(url) as conn:
        conn.execute(HISTORY_SCHEMA)  # GET joins agent_runs for the cost
    store = ApiStore(url)
    store.setup()
    store.prefix = f"test-{uuid.uuid4().hex[:8]}"
    yield store
    with psycopg.connect(url) as conn:
        like = store.prefix + "%"
        conn.execute("DELETE FROM api_decisions WHERE run_id LIKE %s", [like])
        conn.execute("DELETE FROM api_runs WHERE run_id LIKE %s", [like])


def test_request_id_is_unique_and_status_moves_only_from_the_expected_one(store):
    p = store.prefix
    run, created = store.create_run(f"{p}-a", f"{p}-REQ", "t", "a@example.com", "hi")
    again, created_again = store.create_run(f"{p}-b", f"{p}-REQ", "t", "a@example.com", "hi")
    assert created and not created_again and again["run_id"] == run["run_id"] == f"{p}-a"
    assert run["status"] == "queued"
    assert {"run_id": f"{p}-a", "status": "queued"} in store.unfinished()

    assert store.set_status(f"{p}-a", "running", "queued")
    assert not store.set_status(f"{p}-a", "running", "queued")
    result = {"status": "pending_approval", "approval_request": {"action": "refund", "args": {}}}
    assert store.set_status(f"{p}-a", "paused", "running", result)
    row = store.get(f"{p}-a")
    assert row["status"] == "paused" and row["result"] == result and row["cost_usd"] is None


def test_a_decision_id_is_used_once_and_only_a_paused_run_takes_one(store):
    p = store.prefix
    store.create_run(f"{p}-a", f"{p}-REQ", "t", "a@example.com", "hi")
    with pytest.raises(NotPaused):
        store.add_decision(f"{p}-D1", f"{p}-a", "approve", None, "maria")
    assert store.decision(f"{p}-D1") is None  # nothing kept when the run was not paused

    store.set_status(f"{p}-a", "running", "queued")
    store.set_status(f"{p}-a", "paused", "running", {})
    assert store.add_decision(f"{p}-D1", f"{p}-a", "edit", {"amount": 400.0}, "maria")
    assert not store.add_decision(f"{p}-D1", f"{p}-a", "edit", {"amount": 400.0}, "maria")
    with pytest.raises(NotPaused):  # a second, different decision finds the run running
        store.add_decision(f"{p}-D2", f"{p}-a", "reject", None, "sam")
    row = store.get(f"{p}-a")
    assert row["status"] == "running" and row["decided_by"] == "maria"
    assert store.decision(f"{p}-D1")["args"] == {"amount": 400.0}
