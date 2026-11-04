"""Phase 7: the dashboard API and the trace built from checkpoints. No database or AWS needed."""

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dashboard.app import APPROVER_NAME, SECURITY_HEADERS, create_app
from dashboard.store import build_trace, trace_id
from integrations.approval import NotWaiting

VIEWER = {"Authorization": "Bearer see"}
APPROVER = {"Authorization": "Bearer decide"}
TOKENS = {"viewer": "see", "approver": "decide"}
PENDING = {
    "action": "refund",
    "args": {"order_id": "ORD-1033", "amount": 720.0, "reason": "lost"},
    "reply": "Refunded.",
    "reasons": ["refund over $500"],
    "critique": {"ok": True, "issues": []},
}
RUN = {"run_id": "r1", "request_id": "REQ-1", "status": "pending_approval"}


@dataclass
class FakeStore:
    pending: dict | None = field(default_factory=lambda: dict(PENDING))

    def list_runs(self, status=None, limit=100):
        return [RUN] if status in (None, "pending_approval") else []

    def get_run(self, run_id):
        return RUN if run_id == "r1" else None

    def trace(self, run_id):
        return {
            "customer_email": "a@example.com",
            "request_text": "hello",
            "steps": [],
            "pending": self.pending,
            "outcome": None,
        }


@pytest.fixture
def sent():
    return []


@pytest.fixture
def api(sent):
    app = create_app(
        FakeStore(), lambda request_id, answer: sent.append((request_id, answer)), TOKENS
    )
    return TestClient(app)


def test_every_data_route_needs_a_token(api):
    for method, path in (
        ("get", "/api/me"),
        ("get", "/api/runs"),
        ("get", "/api/runs/r1"),
        ("post", "/api/runs/r1/decision"),
    ):
        assert getattr(api, method)(path).status_code == 401
        headers = {"Authorization": "Bearer wrong"}
        assert getattr(api, method)(path, headers=headers).status_code == 401


def test_an_unset_token_opens_nothing():
    app = create_app(FakeStore(), lambda *_: None, {"viewer": "", "approver": ""})
    assert TestClient(app).get("/api/runs", headers={"Authorization": "Bearer "}).status_code == 401


def test_health_stays_open_for_the_probes(api):
    assert api.get("/health").status_code == 200


def test_viewer_reads_but_cannot_decide(api, sent):
    assert api.get("/api/runs", headers=VIEWER).json() == [RUN]
    response = api.post("/api/runs/r1/decision", json={"decision": "approve"}, headers=VIEWER)
    assert response.status_code == 403 and sent == []


def test_run_detail_has_the_langfuse_trace_id(api):
    body = api.get("/api/runs/r1", headers=VIEWER).json()
    assert body["trace_id"] == trace_id("r1") and body["pending"]["action"] == "refund"


def test_approver_decision_reaches_step_functions_under_the_tokens_name(api, sent):
    response = api.post("/api/runs/r1/decision", json={"decision": "approve"}, headers=APPROVER)
    assert response.status_code == 202
    assert sent == [("REQ-1", {"decision": "approve", "by": APPROVER_NAME})]


def test_a_request_cannot_choose_who_decided(api, sent):
    body = {"decision": "approve", "by": "someone-else"}
    response = api.post("/api/runs/r1/decision", json=body, headers=APPROVER)
    assert response.status_code == 422 and sent == []


def test_edit_sends_the_checked_full_arguments(api, sent):
    body = {"decision": "edit", "args": {"amount": 450}}
    assert api.post("/api/runs/r1/decision", json=body, headers=APPROVER).status_code == 202
    assert sent[0][1]["args"] == {"order_id": "ORD-1033", "amount": 450.0, "reason": "lost"}


@pytest.mark.parametrize(
    "args",
    [
        {"amount": -5},
        {"amount": "lots"},
        {"order_id": "ORD-1001"},
        {"approved_by": "owner"},
        {},
    ],
)
def test_bad_edits_never_reach_the_worker(api, sent, args):
    body = {"decision": "edit", "args": args}
    assert api.post("/api/runs/r1/decision", json=body, headers=APPROVER).status_code == 422
    assert sent == []


def test_only_edits_take_arguments(api, sent):
    body = {"decision": "approve", "args": {"amount": 1}}
    assert api.post("/api/runs/r1/decision", json=body, headers=APPROVER).status_code == 422
    assert sent == []


def test_a_run_that_is_not_paused_cannot_be_decided(sent):
    app = create_app(FakeStore(pending=None), lambda *_: sent.append(1), TOKENS)
    response = TestClient(app).post(
        "/api/runs/r1/decision", json={"decision": "approve"}, headers=APPROVER
    )
    assert response.status_code == 409 and sent == []


def test_a_second_decision_gets_a_conflict():
    def already_sent(request_id, answer):
        raise NotWaiting("REQ-1 is not waiting for approval")

    app = create_app(FakeStore(), already_sent, TOKENS)
    response = TestClient(app).post(
        "/api/runs/r1/decision", json={"decision": "reject"}, headers=APPROVER
    )
    assert response.status_code == 409


def test_unknown_run_is_404(api):
    assert api.get("/api/runs/nope", headers=VIEWER).status_code == 404


def test_security_headers_are_on_every_response(api):
    response = api.get("/api/runs")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_built_app_is_served_without_a_token(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html>ok</html>")
    app = create_app(FakeStore(), lambda *_: None, TOKENS, tmp_path)
    response = TestClient(app).get("/")
    assert response.status_code == 200 and "Content-Security-Policy" in response.headers


def tuple_of(ts, values, writes=None):
    return SimpleNamespace(checkpoint={"ts": ts, "channel_values": values}, pending_writes=writes)


BASE = {"customer_email": "a@example.com", "request_text": "<img src=x onerror=alert(1)>"}


def test_trace_of_a_paused_run():
    tool_turn = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "name": "lookup_order", "input": {"order_id": "ORD-1033"}},
            {"type": "text", "text": "ok"},
        ],
    }
    checkpoints = [
        tuple_of("2026-11-04T10:00:00+00:00", {**BASE, "branch:to:planner": None}),
        tuple_of(
            "2026-11-04T10:00:02+00:00",
            {**BASE, "plan": {"summary": "s"}, "branch:to:retriever": None},
        ),
        tuple_of(
            "2026-11-04T10:00:03+00:00",
            {
                **BASE,
                "docs": [{"doc_id": "POL-007", "title": "Lost orders", "text": "long policy"}],
                "branch:to:tool_agent": None,
            },
        ),
        tuple_of(
            "2026-11-04T10:00:07+00:00",
            {
                **BASE,
                "proposal": {"action": "refund"},
                "agent_messages": [{"role": "user", "content": "x"}, tool_turn],
                "branch:to:approval_gate": None,
            },
            [("task", "__interrupt__", [SimpleNamespace(value=PENDING)])],
        ),
    ]
    trace = build_trace(checkpoints)
    assert [s["node"] for s in trace["steps"]] == [
        "planner",
        "retriever",
        "tool_agent",
        "approval_gate",
    ]
    assert trace["steps"][0]["seconds"] == 2.0
    assert trace["steps"][1]["output"]["docs"] == [{"doc_id": "POL-007", "title": "Lost orders"}]
    assert trace["steps"][2]["output"]["tool_calls"] == [
        {"name": "lookup_order", "input": {"order_id": "ORD-1033"}}
    ]
    assert trace["steps"][3]["seconds"] is None
    assert trace["pending"] == PENDING and trace["outcome"] is None
    assert trace["request_text"] == BASE["request_text"]


def test_trace_reads_a_pause_saved_as_a_plain_dict():
    """The dashboard image has no langgraph core, so a saved Interrupt loads as a dict."""
    saved = {"value": PENDING, "id": "abc"}
    checkpoints = [
        tuple_of(
            "2026-11-04T10:00:00+00:00",
            {**BASE, "branch:to:approval_gate": None},
            [("task", "__interrupt__", [saved])],
        )
    ]
    assert build_trace(checkpoints)["pending"] == PENDING


def test_reply_with_no_action_cannot_be_edited():
    pending = {**PENDING, "action": None, "args": {}}
    client = TestClient(create_app(FakeStore(pending=pending), lambda *_: None, TOKENS))
    body = {"decision": "edit", "args": {"amount": 1}}
    assert client.post("/api/runs/r1/decision", json=body, headers=APPROVER).status_code == 422


def test_trace_of_a_finished_run_has_no_pending_decision():
    outcome = {"status": "done", "executed": True}
    checkpoints = [
        tuple_of("2026-11-04T10:00:00+00:00", {**BASE, "branch:to:commit": None}),
        tuple_of("2026-11-04T10:00:01+00:00", {**BASE, "outcome": outcome}),
    ]
    trace = build_trace(checkpoints)
    assert trace["pending"] is None and trace["outcome"] == outcome
    assert trace["steps"][0]["output"] == {"outcome": outcome}


def test_trace_of_a_run_with_no_checkpoints_is_empty():
    assert build_trace([]) == {
        "customer_email": None,
        "request_text": None,
        "steps": [],
        "pending": None,
        "outcome": None,
    }
