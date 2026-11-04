"""Jira client and worker against a fake Jira (httpx.MockTransport). The agent runs with
FakeProvider, a fake retriever and the in-process mock API. No network, no database."""

import asyncio
import json

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import build_graph
from integrations.jira import PROCESSED_LABEL, JiraClient
from integrations.worker import ESCALATED, PENDING, RESOLVED, Worker, plan_update
from tests.test_agent import (
    ALICE,
    FARID,
    final,
    lookup,
    make_deps,
    plan,
    refund,
    review,
    written_actions,
)

STATUSES = {"Waiting for support", "Waiting for customer", "Pending", "Resolved"}  # no Escalated


class FakeJira:
    def __init__(self):
        self.issues = {}
        self.requests = []

    def add(self, key, email, summary, description=""):
        self.issues[key] = {
            "email": email,
            "summary": summary,
            "description": description,
            "labels": [],
            "status": "Waiting for support",
            "comments": [],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        parts = path.strip("/").split("/")
        if path == "/rest/api/3/search/jql":
            jql = request.url.params["jql"]
            assert PROCESSED_LABEL in jql and "statusCategory != Done" in jql
            keys = [
                k
                for k, i in self.issues.items()
                if PROCESSED_LABEL not in i["labels"] and i["status"] != "Resolved"
            ]
            return httpx.Response(200, json={"issues": [{"key": k} for k in keys]})
        key = parts[3] if path.startswith("/rest/servicedeskapi/") else parts[4]
        issue = self.issues[key]
        if path == f"/rest/servicedeskapi/request/{key}":
            values = [
                {"fieldId": "summary", "value": issue["summary"]},
                {"fieldId": "description", "value": issue["description"]},
            ]
            reporter = {"emailAddress": issue["email"]}
            return httpx.Response(200, json={"reporter": reporter, "requestFieldValues": values})
        if path.endswith("/comment"):
            body = json.loads(request.content)
            issue["comments"].append((body["body"], body["public"]))
            return httpx.Response(201, json={})
        if path == f"/rest/api/3/issue/{key}" and method == "PUT":
            for change in json.loads(request.content)["update"]["labels"]:
                issue["labels"].append(change["add"])
            return httpx.Response(204)
        if path.endswith("/transitions") and method == "GET":
            available = [{"id": str(n), "to": {"name": s}} for n, s in enumerate(sorted(STATUSES))]
            return httpx.Response(200, json={"transitions": available})
        if path.endswith("/transitions") and method == "POST":
            chosen = int(json.loads(request.content)["transition"]["id"])
            issue["status"] = sorted(STATUSES)[chosen]
            return httpx.Response(204)
        return httpx.Response(404)


class FakeHistory:
    def __init__(self):
        self.rows = []

    def record(self, row):
        self.rows.append(row)

    def update(self, run_id, **fields):
        self.rows.append({"run_id": run_id, **fields})

    def ticket_for(self, run_id):
        return next((r["ticket_key"] for r in self.rows if r["run_id"] == run_id), None)


def setup(replies):
    fake = FakeJira()
    jira = JiraClient(
        "https://x.atlassian.net", "me@e.com", "token", "SUP", httpx.MockTransport(fake.handler)
    )
    deps = make_deps(replies)
    graph = build_graph(deps, InMemorySaver())
    meta = {"model": "claude-haiku-5-5", "commit": "abc", "dirty": False}
    return fake, Worker(graph, deps, jira, FakeHistory(), meta)


def test_client_reads_ticket_with_customer_email_and_basic_auth():
    fake, worker = setup([])
    fake.add("SUP-1", ALICE, "Broken mug", "My mug from ORD-1001 arrived broken.")
    tickets = asyncio.run(worker.jira.new_tickets())
    assert [(t.key, t.customer_email) for t in tickets] == [("SUP-1", ALICE)]
    assert tickets[0].text == "Broken mug\n\nMy mug from ORD-1001 arrived broken."
    assert fake.requests[0].headers["Authorization"].startswith("Basic ")


def test_move_to_returns_false_when_workflow_has_no_such_status():
    fake, worker = setup([])
    fake.add("SUP-1", ALICE, "x")
    assert asyncio.run(worker.jira.move_to("SUP-1", ESCALATED)) is False
    assert asyncio.run(worker.jira.move_to("SUP-1", "resolved")) is True
    assert fake.issues["SUP-1"]["status"] == RESOLVED


def test_small_refund_ticket_gets_public_reply_and_is_resolved():
    replies = [plan(), lookup("ORD-1001"), refund("ORD-1001", 65.0), final(reply="Refunded.")]
    replies.append(review())
    fake, worker = setup(replies)
    fake.add("SUP-1", ALICE, "Broken mug", "My mug from ORD-1001 arrived broken.")
    assert asyncio.run(worker.poll_once()) == 1
    issue = fake.issues["SUP-1"]
    assert issue["comments"] == [("Refunded.", True)]
    assert issue["status"] == RESOLVED and PROCESSED_LABEL in issue["labels"]
    assert written_actions()[0]["action"] == "refund"
    row = worker.history.rows[0]
    assert row["source"] == "jira" and row["ticket_key"] == "SUP-1" and row["executed"]
    assert row["customer_email"] == ALICE and row["amount"] == 65.0
    assert asyncio.run(worker.poll_once()) == 0  # labelled tickets are not picked up again


def test_large_refund_gets_internal_note_then_reply_after_approval():
    replies = [plan(), lookup("ORD-1011"), refund("ORD-1011", 649.0), final(), review()]
    fake, worker = setup(replies)
    fake.add("SUP-2", FARID, "Return", "Espresso machine ORD-1011 arrived broken.")
    out = asyncio.run(worker.handle(asyncio.run(worker.jira.ticket("SUP-2"))))
    issue = fake.issues["SUP-2"]
    note, public = issue["comments"][0]
    assert not public and "refund over $500" in note and out["run_id"] in note
    assert issue["status"] == PENDING and written_actions() == []
    assert worker.history.rows[0]["status"] == "pending_approval"

    asyncio.run(worker.resume(out["run_id"], {"decision": "approve", "by": "owner"}))
    assert issue["comments"][1] == ("Done.\n\nUpdate: our team has approved this.", True)
    assert issue["status"] == RESOLVED
    assert written_actions()[0]["approved_by"] == "owner"
    assert worker.history.rows[1] == {
        "run_id": out["run_id"],
        "status": "done",
        "approved_by": "owner",
        "executed": True,
        "amount": 649.0,
        "reason": "damaged_or_defective",
    }


def test_agent_error_posts_internal_note_and_is_recorded():
    fake, worker = setup([plan()])  # the provider runs out of replies -> the run fails
    fake.add("SUP-3", ALICE, "Help", "My mug from ORD-1001 arrived broken.")
    asyncio.run(worker.poll_once())
    note, public = fake.issues["SUP-3"]["comments"][0]
    assert not public and note.startswith("Agent error")
    assert worker.history.rows[0]["status"] == "error"


def test_plan_update_for_each_outcome():
    done = {"run_id": "r", "status": "done", "kind": "answer", "action": None, "reply": "Hi"}
    assert plan_update(done).status == RESOLVED and plan_update(done).public
    clarify = {**done, "kind": "clarification"}
    assert plan_update(clarify).status == "Waiting for customer"
    escalated = {**done, "action": "escalate"}
    assert plan_update(escalated).status == ESCALATED
    rejected = {**done, "status": "rejected", "action": "refund", "approval": {"by": "owner"}}
    assert not plan_update(rejected).public and plan_update(rejected).status is None
    blocked = {**done, "status": "blocked", "api_result": None}
    assert not plan_update(blocked).public
    approved = {**done, "approval": {"decision": "approve", "by": "owner"}}
    assert plan_update(approved).comment.endswith("our team has approved this.")
    edited = {**done, "approval": {"decision": "edit"}, "args": {"amount": 100.0}}
    assert plan_update(edited).comment.endswith("The final amount is $100.00.")
