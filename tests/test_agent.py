"""Agent nodes and the full graph, with FakeProvider, a fake retriever and the real mock API
in-process. No network, no database."""

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent import nodes
from agent.graph import build_graph, resume, start
from agent.llm import BudgetExceeded, FakeProvider, LLMClient
from agent.llm.tracing import NullTracer
from agent.nodes import AgentError, Deps
from agent.tools import MockApi
from mock_api.app import app, store

ALICE = "alice.ng@example.com"  # owns ORD-1001 ($65, delivered)
FARID = "farid.haddad@example.com"  # owns ORD-1011 ($649, delivered)


@dataclass
class Hit:
    doc_id: str
    title: str
    text: str


class FakeRetriever:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return [Hit("POL-006", "Damaged items", "Damaged items within 30 days: full refund.")]


def plan(query="damaged item refund"):
    return json.dumps(
        {"summary": "s", "order_ids": [], "search_query": query, "steps": [], "risks": []}
    )


def final(kind="answer", confidence=0.9, reply="Done."):
    return json.dumps(
        {"reply": reply, "kind": kind, "cited_policies": ["POL-006"], "confidence": confidence}
    )


def review(ok=True, issues=()):
    return json.dumps({"ok": ok, "issues": list(issues)})


def lookup(order_id):
    return {"tool": "lookup_order", "input": {"order_id": order_id}}


def refund(order_id, amount, reason="damaged_or_defective"):
    return {
        "tool": "issue_refund",
        "input": {"order_id": order_id, "amount": amount, "reason": reason},
    }


def make_deps(replies, max_iterations=10):
    store.reset()
    llm = LLMClient(
        provider=FakeProvider(replies=list(replies)),
        tracer=NullTracer(),
        model="claude-haiku-5-5",
        max_tokens_per_request=8000,
        max_spend_per_run_usd=0.05,
        max_iterations=max_iterations,
    )
    api = MockApi("http://mock", transport=httpx.ASGITransport(app=app))
    return Deps(llm=llm, retriever=FakeRetriever(), api=api)


def request(email=ALICE, text="My mug from ORD-1001 arrived broken."):
    return {"id": "REQ-T01", "customer_email": email, "request_text": text}


def run_graph(deps, req=None, answer=None, critic=True):
    async def go():
        graph = build_graph(deps, InMemorySaver(), critic=critic)
        out = await start(graph, req or request(), run_id="run-1")
        if answer is not None:
            out = await resume(graph, "run-1", answer)
        return out

    return asyncio.run(go())


def state(**extra):
    base = {
        "run_id": "r",
        "request_id": "REQ-T01",
        "customer_email": ALICE,
        "request_text": "My mug from ORD-1001 arrived broken.",
        "orders": {},
        "plan": json.loads(plan()),
        "docs": [{"doc_id": "POL-006", "title": "t", "text": "x"}],
    }
    return {**base, **extra}


def written_actions():
    return [a for a in store.actions if a["action"] != "lookup_order"]


# ---------------------------------------------------------------- nodes


def test_planner_returns_plan():
    deps = make_deps([plan("lost package")])
    update = asyncio.run(nodes.planner(state(), deps))
    assert update["plan"]["search_query"] == "lost package"
    assert deps.llm.provider.calls[0]["output_schema"] == nodes.PLAN_SCHEMA


def test_planner_rejects_non_json():
    deps = make_deps(["not json"])
    with pytest.raises(AgentError, match="planner"):
        asyncio.run(nodes.planner(state(), deps))


def test_retriever_uses_plan_query():
    deps = make_deps([])
    update = asyncio.run(nodes.retriever(state(), deps))
    assert deps.retriever.queries == ["damaged item refund"]
    assert update["docs"][0]["doc_id"] == "POL-006"


def test_tool_agent_proposes_refund_without_executing_it():
    deps = make_deps([lookup("ORD-1001"), refund("ORD-1001", 65.0), final()])
    update = asyncio.run(nodes.tool_agent(state(), deps))
    assert update["proposal"]["action"] == "refund"
    assert update["proposal"]["args"] == {
        "order_id": "ORD-1001",
        "amount": 65.0,
        "reason": "damaged_or_defective",
    }
    assert "ORD-1001" in update["orders"]
    assert written_actions() == []


def test_tool_agent_hides_other_customers_orders():
    deps = make_deps([lookup("ORD-1011"), final(kind="refusal")])
    update = asyncio.run(nodes.tool_agent(state(), deps))
    tool_result = deps.llm.provider.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "No order ORD-1011" in tool_result["content"]
    assert update["orders"] == {}
    assert update["proposal"]["action"] is None


def test_tool_agent_refuses_write_on_unowned_order():
    deps = make_deps([refund("ORD-1011", 649.0), final(kind="refusal")])
    update = asyncio.run(nodes.tool_agent(state(), deps))
    assert update["proposal"]["action"] is None


def test_tool_agent_rejects_bad_arguments_then_accepts_fix():
    deps = make_deps([refund("ORD-1001", -5), refund("ORD-1001", 65.0), final()])
    update = asyncio.run(nodes.tool_agent(state(), deps))
    first_result = deps.llm.provider.calls[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] and "amount" in first_result["content"]
    assert update["proposal"]["args"]["amount"] == 65.0


def test_tool_agent_allows_one_write_only():
    deps = make_deps(
        [refund("ORD-1001", 65.0), {"tool": "close_ticket", "input": {"resolution": "x"}}, final()]
    )
    update = asyncio.run(nodes.tool_agent(state(), deps))
    second_result = deps.llm.provider.calls[2]["messages"][-1]["content"][0]
    assert second_result["is_error"] and "Only one write action" in second_result["content"]
    assert update["proposal"]["action"] == "refund"


def test_tool_agent_stops_after_max_turns():
    deps = make_deps([lookup("ORD-1001")] * nodes.MAX_TOOL_TURNS)
    with pytest.raises(AgentError, match="no final answer"):
        asyncio.run(nodes.tool_agent(state(), deps))


def test_critic_returns_review():
    deps = make_deps([review(False, ["amount too high"])])
    proposal = {"action": "refund", "args": {}, "reply": "r", "confidence": 0.9}
    update = asyncio.run(nodes.critic(state(proposal=proposal), deps))
    assert update["critique"] == {"ok": False, "issues": ["amount too high"]}


def test_after_critic_allows_one_revision():
    assert nodes.after_critic({"critique": {"ok": False}, "revisions": 0}) == "tool_agent"
    assert nodes.after_critic({"critique": {"ok": False}, "revisions": 1}) == "approval_gate"
    assert nodes.after_critic({"critique": {"ok": True}, "revisions": 0}) == "approval_gate"


def test_approval_reasons():
    ok = {"ok": True, "issues": []}
    big = {"action": "refund", "args": {"amount": 649.0}, "confidence": 0.9}
    unsure = {"action": "escalate", "args": {}, "confidence": 0.5}
    fine = {"action": "refund", "args": {"amount": 65.0}, "confidence": 0.9}
    assert nodes.approval_reasons({"proposal": big, "critique": ok}) == ["refund over $500"]
    assert "confidence" in nodes.approval_reasons({"proposal": unsure, "critique": ok})[0]
    assert nodes.approval_reasons({"proposal": fine, "critique": ok}) == []


def test_reply_with_no_action_pauses_only_for_a_critic_objection():
    reply_only = {"action": None, "args": {}, "confidence": 0.1}
    assert nodes.approval_reasons({"proposal": reply_only, "critique": review_ok()}) == []
    objected = {"proposal": reply_only, "critique": {"ok": False, "issues": ["x"]}}
    assert nodes.approval_reasons(objected) == [nodes.CRITIC_REASON]


def review_ok():
    return {"ok": True, "issues": []}


# ---------------------------------------------------------------- full graph


def test_small_refund_runs_end_to_end_without_approval():
    deps = make_deps([plan(), lookup("ORD-1001"), refund("ORD-1001", 65.0), final(), review()])
    out = run_graph(deps)
    assert out["status"] == "done" and out["executed"]
    assert out["sent_to_approval"] is False
    assert written_actions()[0]["action"] == "refund"
    assert out["retrieved"] == ["POL-006"]


def test_answer_only_takes_no_write_action():
    deps = make_deps([plan(), lookup("ORD-1001"), final(), review()])
    out = run_graph(deps)
    assert out["action"] is None and out["status"] == "done"
    assert out["looked_up"] == ["ORD-1001"]
    assert written_actions() == []


def test_large_refund_pauses_then_runs_after_approval():
    replies = [plan(), lookup("ORD-1011"), refund("ORD-1011", 649.0), final(), review()]
    req = request(FARID, "Espresso machine ORD-1011 arrived broken.")

    paused = run_graph(make_deps(replies), req)
    assert paused["status"] == "pending_approval"
    assert paused["approval_request"]["reasons"] == ["refund over $500"]
    assert written_actions() == []

    out = run_graph(make_deps(replies), req, answer={"decision": "approve", "by": "owner"})
    assert out["status"] == "done" and out["sent_to_approval"]
    assert written_actions()[0]["approved_by"] == "owner"


def test_rejected_action_is_not_executed():
    replies = [plan(), lookup("ORD-1011"), refund("ORD-1011", 649.0), final(), review()]
    req = request(FARID, "Espresso machine ORD-1011 arrived broken.")
    out = run_graph(make_deps(replies), req, answer={"decision": "reject", "by": "owner"})
    assert out["status"] == "rejected" and not out["executed"]
    assert written_actions() == []


def test_edit_validates_new_arguments():
    replies = [plan(), lookup("ORD-1011"), refund("ORD-1011", 649.0), final(), review()]
    req = request(FARID, "Espresso machine ORD-1011 arrived broken.")
    edited = {"order_id": "ORD-1011", "amount": 100.0, "reason": "damaged_or_defective"}
    out = run_graph(make_deps(replies), req, {"decision": "edit", "by": "owner", "args": edited})
    assert out["executed"] and written_actions()[0]["amount"] == 100.0


def test_critic_rejection_gets_one_revision():
    replies = [
        plan(),
        lookup("ORD-1001"),
        refund("ORD-1001", 65.0),
        final(),
        review(False, ["should ask first"]),
        final(kind="clarification", reply="Can you send a photo?"),
        review(),
    ]
    deps = make_deps(replies)
    out = run_graph(deps)
    revision_call = deps.llm.provider.calls[5]["messages"]
    assert len(revision_call) == 7 and "should ask first" in revision_call[-1]["content"]
    assert out["revisions"] == 1
    assert out["action"] is None and out["kind"] == "clarification"
    assert written_actions() == []


def test_second_critic_rejection_goes_to_human():
    replies = [
        plan(),
        refund("ORD-1001", 65.0),
        final(),
        review(False, ["a"]),
        refund("ORD-1001", 65.0),
        final(),
        review(False, ["b"]),
    ]
    out = run_graph(make_deps(replies))
    assert out["status"] == "pending_approval"
    assert "critic" in out["approval_request"]["reasons"][-1]


def false_handoff_replies():
    claim = "I've passed this to our team."
    return [
        plan(),
        final(reply=claim),
        review(False, ["no escalation was made"]),
        final(reply=claim),
        review(False, ["still no escalation"]),
    ]


def test_reply_the_critic_still_rejects_is_not_sent_without_a_human():
    out = run_graph(make_deps(false_handoff_replies()))
    assert out["status"] == "pending_approval"
    assert out["approval_request"]["action"] is None
    assert out["approval_request"]["reasons"] == [nodes.CRITIC_REASON]


def test_rejected_reply_is_recorded_as_rejected():
    deps = make_deps(false_handoff_replies())
    out = run_graph(deps, answer={"decision": "reject", "by": "owner"})
    assert out["status"] == "rejected" and not out["executed"]
    assert written_actions() == []


def test_approved_reply_finishes_with_no_action():
    deps = make_deps(false_handoff_replies())
    out = run_graph(deps, answer={"decision": "approve", "by": "owner"})
    assert out["status"] == "done" and out["sent_to_approval"]
    assert written_actions() == []


def test_reply_with_no_action_cannot_be_edited():
    deps = make_deps(false_handoff_replies())
    with pytest.raises(AgentError, match="nothing to edit"):
        run_graph(deps, answer={"decision": "edit", "by": "owner", "args": {}})


def test_failed_lookups_reach_the_critic():
    replies = [plan(), lookup("ORD-9999"), lookup("1234"), final(kind="clarification"), review()]
    deps = make_deps(replies)
    out = run_graph(deps)
    critic_prompt = deps.llm.provider.calls[-1]["messages"][0]["content"]
    assert out["failed_lookups"] == ["ORD-9999", "1234"]
    assert 'found no order for this customer: ["ORD-9999", "1234"]' in critic_prompt
    assert "Orders found for this customer: {}" in critic_prompt


def test_another_customers_order_counts_as_a_failed_lookup():
    deps = make_deps([plan(), lookup("ORD-1011"), final(kind="clarification"), review()])
    out = run_graph(deps)
    assert out["failed_lookups"] == ["ORD-1011"] and out["looked_up"] == []


def test_empty_reply_gets_another_turn():
    replies = [
        plan(),
        final(reply=""),
        lookup("ORD-1001"),
        refund("ORD-1001", 65.0),
        final(),
        review(),
    ]
    deps = make_deps(replies)
    out = run_graph(deps)
    assert out["reply"] == "Done." and out["executed"]
    assert written_actions()[0]["action"] == "refund"
    assert nodes.EMPTY_REPLY_NOTE in str(deps.llm.provider.calls[2]["messages"])


def test_found_order_is_not_a_failed_lookup():
    deps = make_deps([plan(), lookup("ORD-1001"), final(), review()])
    assert run_graph(deps)["failed_lookups"] == []


def test_run_stops_at_max_llm_calls():
    deps = make_deps([plan(), lookup("ORD-1001"), lookup("ORD-1001")], max_iterations=2)
    with pytest.raises(BudgetExceeded, match="max iterations"):
        run_graph(deps)


def test_graph_without_critic_skips_review():
    deps = make_deps([plan(), lookup("ORD-1001"), refund("ORD-1001", 65.0), final()])
    out = run_graph(deps, critic=False)
    assert out["status"] == "done" and out["executed"]
    assert len(deps.llm.provider.calls) == 4


DECLINED = {"refusal": True}


def test_declined_calls_end_in_a_refusal_without_actions():
    deps = make_deps([DECLINED, DECLINED, DECLINED])
    out = run_graph(deps)
    assert out["status"] == "done" and out["action"] is None
    assert out["kind"] == "refusal" and out["reply"] == nodes.DECLINED_REPLY
    assert deps.retriever.queries == [request()["request_text"]]
    assert written_actions() == []


def test_declined_review_of_a_write_goes_to_human():
    deps = make_deps([refund("ORD-1001", 65.0), final(), DECLINED])
    update = asyncio.run(nodes.tool_agent(state(), deps))
    review_update = asyncio.run(nodes.critic(state(**update), deps))
    assert review_update["critique"]["ok"] is False
