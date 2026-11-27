"""The HTTP API for GovernedDesk. The agent runs with FakeProvider (no LLM calls), a fake retriever,
the in-process mock API and an in-memory checkpointer; MemoryStore stands in for Postgres and a
fake receiver (httpx.MockTransport) for GovernedDesk's webhook endpoint."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import build_graph
from api import signing
from api.app import create_app
from api.runner import RESTARTED, Runner
from api.store import NotPaused
from api.webhooks import Webhooks
from integrations.worker import Worker
from tests.test_agent import ALICE, FARID, final, lookup, make_deps, plan, refund, review
from tests.test_agent import written_actions as executed_actions
from tests.test_jira import FakeHistory

SECRET = "test-hmac-secret"
TENANT = "brightcart"
HOOK_URL = "https://governeddesk.test/hooks/ops"
BIG_REFUND = [plan(), lookup("ORD-1011"), refund("ORD-1011", 649.0), final(), review()]
SMALL_REFUND = [plan(), lookup("ORD-1001"), refund("ORD-1001", 65.0), final(), review()]


class MemoryStore:
    """ApiStore's behaviour without Postgres (tests/test_api_store.py checks the real one)."""

    def __init__(self, history: FakeHistory):
        self.runs, self.decisions, self.history = {}, {}, history

    def create_run(self, run_id, request_id, tenant_id, email, text):
        for run in self.runs.values():
            if run["request_id"] == request_id:
                return self.get(run["run_id"]), False
        self.runs[run_id] = {
            "run_id": run_id,
            "request_id": request_id,
            "tenant_id": tenant_id,
            "customer_email": email,
            "request_text": text,
            "status": "queued",
            "result": None,
            "error": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        return self.get(run_id), True

    def get(self, run_id):
        if run_id not in self.runs:
            return None
        rows = [r for r in self.history.rows if r["run_id"] == run_id and "cost_usd" in r]
        decided = [d for d in self.decisions.values() if d["run_id"] == run_id]
        return {
            **self.runs[run_id],
            "cost_usd": rows[-1]["cost_usd"] if rows else None,
            "decided_by": decided[-1]["decided_by"] if decided else None,
        }

    def set_status(self, run_id, status, expect, result=None, error=""):
        run = self.runs[run_id]
        if run["status"] != expect:
            return False
        run["status"] = status
        run["result"] = json.loads(json.dumps(result)) if result is not None else run["result"]
        run["error"] = error or None
        return True

    def decision(self, decision_id):
        return self.decisions.get(decision_id)

    def add_decision(self, decision_id, run_id, decision, args, decided_by):
        if decision_id in self.decisions:
            return False
        if self.runs[run_id]["status"] != "paused":
            raise NotPaused(run_id)
        self.decisions[decision_id] = {
            "decision_id": decision_id,
            "run_id": run_id,
            "decision": decision,
            "args": args,
            "decided_by": decided_by,
        }
        self.runs[run_id]["status"] = "running"
        return True

    def unfinished(self):
        return [
            {"run_id": r["run_id"], "status": r["status"]}
            for r in self.runs.values()
            if r["status"] in ("queued", "running")
        ]


class Receiver:
    """GovernedDesk's webhook endpoint. down=True: every call fails to connect."""

    def __init__(self, down=False):
        self.down, self.calls = down, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectError("receiver is down")
        return httpx.Response(204)

    def events(self):
        return [json.loads(r.content) for r in self.calls]


class Setup:
    def __init__(self, replies, receiver=None):
        self.deps = make_deps(replies)
        self.history = FakeHistory()
        self.store = MemoryStore(self.history)
        self.receiver = receiver or Receiver()
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.receiver.handler))
        self.webhooks = Webhooks(HOOK_URL, SECRET, client, backoff_s=(0, 0))
        self.resumes = 0
        worker = Worker(
            build_graph(self.deps, InMemorySaver()),
            self.deps,
            None,
            self.history,
            {"model": "claude-haiku-5-5", "commit": "abc", "dirty": False},
        )
        original = worker.resume

        async def counted(run_id, answer):
            self.resumes += 1
            return await original(run_id, answer)

        worker.resume = counted
        self.runner = Runner(worker, self.store, self.webhooks)
        self.app = create_app(self.store, TENANT, SECRET, self.runner)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")


def signed(body: bytes = b"", secret=SECRET, now=None) -> dict:
    return {"Content-Type": "application/json", **signing.signed_headers(secret, body, now)}


async def post(client, path, payload, **sign):
    body = json.dumps(payload).encode()
    return await client.post(path, content=body, headers=signed(body, **sign))


async def get(client, path, **sign):
    return await client.get(path, headers=signed(b"", **sign))


def new_run(request_id="GD-1", email=FARID, text="Espresso machine ORD-1011 arrived broken."):
    return {"request_id": request_id, "tenant_id": TENANT, "customer_email": email, "text": text}


def decision(kind, decision_id="D-1", **extra):
    by = "maria@governeddesk"
    return {"decision": kind, "decided_by": by, "decision_id": decision_id, **extra}


async def paused_run(s: Setup, client) -> str:
    response = await post(client, "/runs", new_run())
    assert response.status_code == 202 and response.json()["status"] == "queued"
    await s.runner.drain()
    run_id = response.json()["run_id"]
    assert (await get(client, f"/runs/{run_id}")).json()["status"] == "paused"
    return run_id


# ---------------------------------------------------------------- signing


def test_signature_round_trip_and_age_limit():
    body, now = b'{"a":1}', time.time()
    headers = signing.signed_headers(SECRET, body, now)
    ts, sig = headers[signing.TIMESTAMP_HEADER], headers[signing.SIGNATURE_HEADER]
    assert sig.startswith("sha256=")
    assert signing.check(SECRET, ts, sig, body, now) is None
    assert signing.check(SECRET, ts, sig, b'{"a":2}', now) == "bad signature"
    assert signing.check("other", ts, sig, body, now) == "bad signature"
    assert signing.check(SECRET, ts, sig, body, now + 301) == "timestamp too old or too far ahead"
    assert signing.check(SECRET, ts, sig, body, now - 301) == "timestamp too old or too far ahead"
    assert signing.check(SECRET, None, sig, body, now) == "missing signature headers"
    assert signing.check(SECRET, "soon", sig, body, now) == "bad timestamp"


def test_app_refuses_to_start_without_tenant_or_secret():
    with pytest.raises(ValueError):
        create_app(None, "", SECRET)
    with pytest.raises(ValueError):
        create_app(None, TENANT, "")


# ---------------------------------------------------------------- auth


def test_health_needs_no_signature():
    async def go():
        async with Setup([]).client() as client:
            response = await client.get("/health")
            assert response.status_code == 200 and response.json() == {"ok": True}
            assert response.headers["X-Content-Type-Options"] == "nosniff"

    asyncio.run(go())


def test_bad_signature_is_401_on_every_route():
    async def go():
        s = Setup([])
        async with s.client() as client:
            for path in ("/runs", "/runs/x/decision"):
                body = json.dumps(new_run()).encode()
                r = await client.post(path, content=body, headers=signed(body, secret="wrong"))
                assert r.status_code == 401 and r.json()["detail"] == "bad signature"
            assert (await get(client, "/runs/x", secret="wrong")).status_code == 401
            assert (await client.post("/runs", json=new_run())).status_code == 401  # unsigned
        assert s.store.runs == {}

    asyncio.run(go())


def test_old_timestamp_is_401():
    async def go():
        s = Setup([])
        async with s.client() as client:
            r = await post(client, "/runs", new_run(), now=time.time() - 301)
            assert r.status_code == 401 and "too old" in r.json()["detail"]
        assert s.store.runs == {}

    asyncio.run(go())


def test_wrong_tenant_is_403():
    async def go():
        s = Setup([])
        async with s.client() as client:
            r = await post(client, "/runs", {**new_run(), "tenant_id": "other-shop"})
            assert r.status_code == 403
        assert s.store.runs == {}

    asyncio.run(go())


# ---------------------------------------------------------------- idempotency


def test_same_request_id_twice_starts_one_run():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            first = await post(client, "/runs", new_run())
            second = await post(client, "/runs", new_run())
            await s.runner.drain()
            third = await post(client, "/runs", new_run())
        assert first.status_code == second.status_code == third.status_code == 202
        assert first.json()["run_id"] == second.json()["run_id"] == third.json()["run_id"]
        assert third.json()["status"] == "paused"
        assert len(s.store.runs) == 1
        assert len(s.history.rows) == 1  # one agent run recorded

    asyncio.run(go())


def test_request_id_reused_for_a_different_request_is_409():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            await post(client, "/runs", new_run())
            r = await post(client, "/runs", new_run(text="Something else entirely"))
            await s.runner.drain()
        assert r.status_code == 409 and len(s.store.runs) == 1

    asyncio.run(go())


def test_same_decision_id_twice_resumes_once():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            first = await post(client, f"/runs/{run_id}/decision", decision("approve"))
            second = await post(client, f"/runs/{run_id}/decision", decision("approve"))
            await s.runner.drain()
            third = await post(client, f"/runs/{run_id}/decision", decision("approve"))
        assert first.status_code == second.status_code == third.status_code == 202
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True and third.json()["duplicate"] is True
        assert third.json()["status"] == "committed"
        assert s.resumes == 1 and len(executed_actions()) == 1

    asyncio.run(go())


def test_decision_id_reused_for_another_decision_is_409():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            await post(client, f"/runs/{run_id}/decision", decision("approve"))
            r = await post(client, f"/runs/{run_id}/decision", decision("reject"))
            await s.runner.drain()
        assert r.status_code == 409 and s.resumes == 1

    asyncio.run(go())


# ---------------------------------------------------------------- decisions


def test_decision_on_a_run_that_is_not_paused_is_409():
    async def go():
        s = Setup(SMALL_REFUND)
        async with s.client() as client:
            r = await post(client, "/runs", new_run(email=ALICE, text="My mug ORD-1001 broke."))
            run_id = r.json()["run_id"]
            await s.runner.drain()
            assert (await get(client, f"/runs/{run_id}")).json()["status"] == "committed"
            r = await post(client, f"/runs/{run_id}/decision", decision("approve"))
            assert r.status_code == 409
            r = await post(client, "/runs/no-such-run/decision", decision("approve", "D-2"))
            assert r.status_code == 404
        assert s.resumes == 0 and s.store.decisions == {}

    asyncio.run(go())


def test_approve_resumes_executes_and_records_decided_by():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            r = await post(client, f"/runs/{run_id}/decision", decision("approve"))
            assert r.status_code == 202 and r.json()["status"] == "running"
            await s.runner.drain()
            run = (await get(client, f"/runs/{run_id}")).json()
        assert run["status"] == "committed" and run["executed"]
        assert run["decided_by"] == "maria@governeddesk"
        assert executed_actions()[0]["approved_by"] == "maria@governeddesk"
        assert s.history.rows[-1] == {
            "run_id": run_id,
            "status": "done",
            "approved_by": "maria@governeddesk",
            "executed": True,
            "amount": 649.0,
            "reason": "damaged_or_defective",
        }

    asyncio.run(go())


def test_edit_resumes_with_the_new_args_and_is_recorded():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            edit = {"action": "refund", "args": {"amount": 400.0}}
            r = await post(client, f"/runs/{run_id}/decision", decision("edit", edited_action=edit))
            assert r.status_code == 202
            await s.runner.drain()
            run = (await get(client, f"/runs/{run_id}")).json()
        assert run["status"] == "committed"
        assert run["proposed_action"]["args"]["amount"] == 400.0
        assert executed_actions()[0]["amount"] == 400.0
        assert s.history.rows[-1]["amount"] == 400.0
        assert s.history.rows[-1]["approved_by"] == "maria@governeddesk"
        assert s.store.decisions["D-1"]["args"]["amount"] == 400.0

    asyncio.run(go())


def test_reject_resumes_executes_nothing_and_is_recorded():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            await post(client, f"/runs/{run_id}/decision", decision("reject"))
            await s.runner.drain()
            run = (await get(client, f"/runs/{run_id}")).json()
        assert run["status"] == "rejected" and not run["executed"]
        assert run["reply_text"] is None  # a rejected draft must not be sent
        assert executed_actions() == []
        assert s.history.rows[-1]["status"] == "rejected"
        assert s.history.rows[-1]["approved_by"] == "maria@governeddesk"

    asyncio.run(go())


def test_bad_edits_are_422_and_leave_the_run_paused():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            path = f"/runs/{run_id}/decision"
            bad = [
                decision("edit", "D-1"),  # no edited_action
                decision("edit", "D-2", edited_action={"action": "close", "args": {}}),
                decision("edit", "D-3", edited_action={"args": {"order_id": "ORD-1001"}}),
                decision("edit", "D-4", edited_action={"args": {"amount": -5}}),
                decision("approve", "D-5", edited_action={"args": {"amount": 1.0}}),
            ]
            for body in bad:
                assert (await post(client, path, body)).status_code == 422, body
            run = (await get(client, f"/runs/{run_id}")).json()
        assert run["status"] == "paused" and s.store.decisions == {} and s.resumes == 0

    asyncio.run(go())


# ---------------------------------------------------------------- read and webhooks


def test_get_run_shows_proposal_critic_notes_reply_and_cost():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            run = (await get(client, f"/runs/{run_id}")).json()
            missing = await get(client, "/runs/nope")
        assert run["proposed_action"] == {
            "action": "refund",
            "args": {"order_id": "ORD-1011", "amount": 649.0, "reason": "damaged_or_defective"},
            "reply": "Done.",
        }
        assert run["critic_notes"] == {"ok": True, "issues": []}
        assert run["reason_for_pause"] == "refund over $500"
        assert run["reply_text"] == "Done."
        assert run["cost_usd"] > 0 and run["tenant_id"] == TENANT and run["request_id"] == "GD-1"
        assert "customer_email" not in run
        assert missing.status_code == 404

    asyncio.run(go())


def test_paused_run_sends_a_signed_webhook():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
        [call] = s.receiver.calls
        assert str(call.url) == HOOK_URL
        ts, sig = call.headers[signing.TIMESTAMP_HEADER], call.headers[signing.SIGNATURE_HEADER]
        assert signing.check(SECRET, ts, sig, call.content) is None
        event = json.loads(call.content)
        assert set(event) == {
            "event_id", "event_type", "run_id", "request_id", "tenant_id", "status",
            "proposed_action", "reason_for_pause", "created_at",
        }  # fmt: skip
        assert event["event_type"] == "run.paused" and event["status"] == "paused"
        assert event["run_id"] == run_id and event["request_id"] == "GD-1"
        assert event["tenant_id"] == TENANT
        assert event["proposed_action"]["action"] == "refund"
        assert event["reason_for_pause"] == "refund over $500"

    asyncio.run(go())


def test_each_end_state_sends_its_webhook():
    async def go():
        s = Setup(BIG_REFUND)
        async with s.client() as client:
            run_id = await paused_run(s, client)
            await post(client, f"/runs/{run_id}/decision", decision("approve"))
            await s.runner.drain()
        assert [e["status"] for e in s.receiver.events()] == ["paused", "committed"]
        assert s.receiver.events()[1]["reason_for_pause"] is None

    asyncio.run(go())


def test_webhook_down_run_still_finishes(caplog):
    async def go():
        s = Setup(SMALL_REFUND, Receiver(down=True))
        async with s.client() as client:
            r = await post(client, "/runs", new_run(email=ALICE, text="My mug ORD-1001 broke."))
            await s.runner.drain()
            run = (await get(client, f"/runs/{r.json()['run_id']}")).json()
        assert run["status"] == "committed" and run["executed"]
        assert len(s.receiver.calls) == 3  # three tries
        return s

    with caplog.at_level(logging.INFO, logger="api.webhooks"):
        asyncio.run(go())
    attempts = [m for m in caplog.messages if "attempt" in m]
    assert len(attempts) == 3 and all("ConnectError" in m for m in attempts)
    assert any("gave up" in m for m in caplog.messages)
    assert not any(SECRET in m for m in caplog.messages)


def test_agent_error_ends_failed_with_a_webhook():
    async def go():
        s = Setup([plan()])  # the fake model runs out of replies, so the run raises
        async with s.client() as client:
            r = await post(client, "/runs", new_run())
            await s.runner.drain()
            run = (await get(client, f"/runs/{r.json()['run_id']}")).json()
        assert run["status"] == "failed" and "FakeProvider" in run["error"]
        assert s.history.rows[0]["status"] == "error"
        assert [e["status"] for e in s.receiver.events()] == ["failed"]

    asyncio.run(go())


def test_runs_left_unfinished_by_a_restart_are_failed():
    async def go():
        s = Setup([])
        s.store.create_run("r1", "GD-1", TENANT, FARID, "x")
        s.store.create_run("r2", "GD-2", TENANT, FARID, "y")
        s.store.runs["r2"]["status"] = "running"
        await s.runner.fail_unfinished()
        assert {r["status"] for r in s.store.runs.values()} == {"failed"}
        assert s.store.runs["r1"]["error"] == RESTARTED
        assert [e["status"] for e in s.receiver.events()] == ["failed", "failed"]

    asyncio.run(go())
