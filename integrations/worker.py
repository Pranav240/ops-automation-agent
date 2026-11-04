"""Jira worker: new service desk requests go through the agent, replies go back to Jira.

For each new ticket: label it (so it is picked up once), run the agent, then
  - finished: post the reply as a public comment and move the ticket on;
  - paused for approval: post an internal note with the proposed action and how to approve;
  - rejected, blocked, failed or error: post an internal note for a human.
Every run is recorded in agent_runs (agent/run_history.py).

Needs Postgres, ingested policies, the mock API and the JIRA_* settings in .env.

Usage:
  python -m integrations.worker --once          # handle the new tickets, then exit
  python -m integrations.worker --interval 30   # keep polling
  python -m integrations.worker --resume RUN_ID --decision approve --by owner
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass

from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.config import db_url, mock_api_url
from agent.graph import build_graph, config, resume, start
from agent.llm import LLMClient
from agent.nodes import Deps
from agent.run_history import RunHistory, from_record
from agent.tools import MockApi
from evals.runner import git_state
from integrations.jira import JiraClient, Ticket
from retrieval.search import HybridRetriever

# Status names from the Jira Service Management service request workflow.
RESOLVED = "Resolved"
WAITING_FOR_CUSTOMER = "Waiting for customer"
ESCALATED = "Escalated"
PENDING = "Pending"


@dataclass
class Update:
    comment: str
    public: bool
    status: str | None


def approval_note(out: dict) -> str:
    """The draft reply was written before the decision, so say what the human decided."""
    approval = out.get("approval", {})
    if approval.get("decision") == "approve":
        return "\n\nUpdate: our team has approved this."
    if approval.get("decision") == "edit":
        amount = out["args"].get("amount")
        final = f" The final amount is ${amount:.2f}." if amount else ""
        return f"\n\nUpdate: our team approved this with changes.{final}"
    return ""


def plan_update(out: dict) -> Update:
    """What to post to Jira for an agent result (see agent.graph.start / resume)."""
    run_id, status = out["run_id"], out["status"]
    if status == "pending_approval":
        request = out["approval_request"]
        return Update(
            f"Agent proposes {request['action']} {json.dumps(request['args'])}.\n"
            f"Needs approval: {'; '.join(request['reasons'])}.\n"
            f"Draft reply: {request['reply']}\n"
            f"Decide: python -m integrations.worker --resume {run_id} "
            "--decision approve|reject|edit --by NAME",
            public=False,
            status=PENDING,
        )
    if status == "done":
        if out["kind"] == "clarification":
            next_status = WAITING_FOR_CUSTOMER
        elif out["action"] == "escalate":
            next_status = ESCALATED
        else:
            next_status = RESOLVED
        return Update(out["reply"] + approval_note(out), public=True, status=next_status)
    if status == "rejected":
        by = out["approval"].get("by")
        return Update(
            f"Proposed {out['action']} was rejected by {by}. Nothing was executed and no reply "
            "was sent; please answer the customer.",
            public=False,
            status=None,
        )
    return Update(
        f"Agent run {run_id} ended with status {status}; nothing was sent to the customer. "
        f"Details: {json.dumps(out.get('api_result'))}",
        public=False,
        status=None,
    )


class Worker:
    def __init__(self, graph, deps: Deps, jira: JiraClient, history: RunHistory, meta: dict):
        self.graph = graph
        self.deps = deps
        self.jira = jira
        self.history = history
        self.meta = meta

    async def existing(self, run_id: str) -> dict | None:
        """This run's result if it already got that far (a queue can deliver a message twice)."""
        snapshot = await self.graph.aget_state(config(run_id))
        if "outcome" in snapshot.values:
            return {"run_id": run_id, **snapshot.values["outcome"]}
        for task in snapshot.tasks:
            if task.interrupts:
                request = task.interrupts[0].value
                return {"run_id": run_id, "status": "pending_approval", "approval_request": request}
        return None

    async def post(self, key: str, update: Update) -> None:
        await self.jira.comment(key, update.comment, update.public)
        if update.status and not await self.jira.move_to(key, update.status):
            print(f"{key}: no transition to '{update.status}' in this workflow; status unchanged")

    async def record(self, ticket: Ticket, run_id: str, latency_s: float, error: str = "") -> None:
        values = (await self.graph.aget_state(config(run_id))).values
        proposal, outcome = values.get("proposal", {}), values.get("outcome")
        budget = self.deps.runs[run_id].budget if run_id in self.deps.runs else None
        rec = {
            **self.meta,
            "system": "A-H",
            "run_id": run_id,
            "request_id": ticket.key,
            "status": "error" if error else outcome["status"] if outcome else "pending_approval",
            "action": proposal.get("action"),
            "args": outcome["args"] if outcome else proposal.get("args", {}),
            "kind": proposal.get("kind"),
            "sent_to_approval": outcome is None or outcome["sent_to_approval"],
            "executed": bool(outcome and outcome["executed"]),
            "revisions": values.get("revisions", 0),
            "llm_calls": budget.calls if budget else 0,
            "cost_usd": budget.spent_usd if budget else 0.0,
            "latency_s": round(latency_s, 3),
        }
        key = ticket.key if ticket.source == "jira" else None
        row = from_record(rec, ticket.source, ticket_key=key, customer_email=ticket.customer_email)
        await asyncio.to_thread(self.history.record, row)

    async def run(self, ticket: Ticket, run_id: str) -> dict:
        """Run the agent for a ticket once, post the result to Jira and record it."""
        began = time.monotonic()
        out = await self.existing(run_id)
        if out is None:
            request = {"id": ticket.key, "customer_email": ticket.customer_email}
            request["request_text"] = ticket.text
            out = await start(self.graph, request, run_id=run_id)
        if ticket.source == "jira":
            await self.post(ticket.key, plan_update(out))
        await self.record(ticket, run_id, time.monotonic() - began)
        print(f"{ticket.key}: {out['status']} {out.get('action') or out.get('kind') or ''}")
        return out

    async def handle(self, ticket: Ticket) -> dict | None:
        await self.jira.mark_processed(ticket.key)  # claim it first so it is never run twice
        run_id, began = uuid.uuid4().hex, time.monotonic()
        try:
            return await self.run(ticket, run_id)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            await self.jira.comment(ticket.key, f"Agent error, please handle: {error}", False)
            await self.record(ticket, run_id, time.monotonic() - began, error=error)
            print(f"{ticket.key}: error {error}")
            return None

    async def poll_once(self) -> int:
        tickets = await self.jira.new_tickets()
        for ticket in tickets:
            await self.handle(ticket)
        return len(tickets)

    async def resume(self, run_id: str, answer: dict) -> dict:
        key = await asyncio.to_thread(self.history.ticket_for, run_id)
        out = await self.existing(run_id)
        if out is None or out["status"] == "pending_approval":
            out = await resume(self.graph, run_id, answer)
        if key:
            await self.post(key, plan_update(out))
        args = out.get("args") or {}
        await asyncio.to_thread(
            self.history.update,
            run_id,
            status=out["status"],
            approved_by=answer["by"],
            executed=out.get("executed", False),
            amount=args.get("amount"),
            reason=args.get("reason"),
        )
        print(f"{key or run_id}: {out['status']} after {answer['decision']} by {answer['by']}")
        return out


async def main(args) -> None:
    llm = LLMClient.from_env()
    api = MockApi(mock_api_url())
    deps = Deps(llm=llm, retriever=HybridRetriever(db_url()), api=api)
    jira = JiraClient.from_env()
    meta = {"model": llm.model, **git_state()}
    async with AsyncPostgresSaver.from_conn_string(db_url()) as saver:
        await saver.setup()
        worker = Worker(build_graph(deps, saver), deps, jira, RunHistory(db_url()), meta)
        if args.resume:
            answer = {"decision": args.decision, "by": args.by}
            if args.args:
                answer["args"] = json.loads(args.args)
            await worker.resume(args.resume, answer)
        elif args.once:
            print(f"handled {await worker.poll_once()} tickets")
        else:
            while True:
                await worker.poll_once()
                await asyncio.sleep(args.interval)
    await jira.aclose()
    await api.aclose()
    llm.tracer.flush()


if __name__ == "__main__":
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--resume")
    parser.add_argument("--decision", choices=["approve", "reject", "edit"])
    parser.add_argument("--by")
    parser.add_argument("--args", help="JSON arguments for --decision edit")
    args = parser.parse_args()
    if args.resume and not (args.decision and args.by):
        parser.error("--resume needs --decision and --by")
    # psycopg's async mode does not work with Windows' default event loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(args), loop_factory=loop_factory)
