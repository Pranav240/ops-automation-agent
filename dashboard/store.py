"""What the dashboard shows: runs from agent_runs and each run's steps from its checkpoints.

Everything here only reads. Checkpoints hold what each graph node produced and when, so the trace
view needs no Langfuse key. The dashboard connects as ops_dashboard, a read-only role.
"""

import hashlib
from datetime import datetime
from decimal import Decimal

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

RUN_COLUMNS = (
    "run_id, source, ticket_key, request_id, status, action, amount, reason, kind, "
    "sent_to_approval, approved_by, executed, revisions, llm_calls, cost_usd, latency_s, "
    "model, created_at"
)
NEXT_NODE = "branch:to:"
INTERRUPT = "__interrupt__"
# What each node adds to the graph state.
NODE_OUTPUT = {
    "planner": ["plan"],
    "retriever": ["docs"],
    "tool_agent": ["proposal"],
    "critic": ["critique"],
    "approval_gate": ["approval"],
    "commit": ["outcome"],
}


def clean_row(row: dict) -> dict:
    def clean(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return {key: clean(value) for key, value in row.items()}


def trace_id(run_id: str) -> str:
    """Langfuse's trace id for this run (same as create_trace_id(seed=run_id))."""
    return hashlib.sha256(run_id.encode()).digest()[:16].hex()


def next_node(values: dict) -> str | None:
    for channel in values:
        if channel.startswith(NEXT_NODE):
            return channel.removeprefix(NEXT_NODE)
    return None


def tool_calls(messages: list[dict]) -> list[dict]:
    calls = []
    for message in messages:
        if message["role"] != "assistant":
            continue
        for block in message["content"]:
            if block.get("type") == "tool_use":
                calls.append({"name": block["name"], "input": block["input"]})
    return calls


def node_output(node: str, values: dict) -> dict:
    output = {name: values.get(name) for name in NODE_OUTPUT.get(node, [])}
    if node == "retriever":
        docs = values.get("docs", [])
        output["docs"] = [{"doc_id": d["doc_id"], "title": d["title"]} for d in docs]
    if node == "tool_agent":
        output["tool_calls"] = tool_calls(values.get("agent_messages", []))
    return output


def seconds_between(start: str, end: str) -> float:
    gap = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return round(gap.total_seconds(), 3)


def interrupt_value(interrupt) -> dict:
    """An Interrupt object, or a plain dict in the dashboard image, which has no langgraph core."""
    return interrupt["value"] if isinstance(interrupt, dict) else interrupt.value


def build_trace(checkpoints: list) -> dict:
    """Turn a run's checkpoints, oldest first, into its request, steps and paused decision.

    Each checkpoint names the node that runs next. The following checkpoint has that node's result
    and its time, so the gap between them is how long the node took.
    """
    steps, pending = [], None
    for index, current in enumerate(checkpoints):
        node = next_node(current.checkpoint["channel_values"])
        if node is None:
            continue
        step = {"node": node, "started_at": current.checkpoint["ts"]}
        if index + 1 < len(checkpoints):
            after = checkpoints[index + 1].checkpoint
            step["seconds"] = seconds_between(current.checkpoint["ts"], after["ts"])
            step["output"] = node_output(node, after["channel_values"])
        else:
            for _, channel, value in current.pending_writes or []:
                if channel == INTERRUPT:
                    pending = interrupt_value(value[0] if isinstance(value, list) else value)
            step["seconds"] = None
            step["output"] = {"waiting_for": pending} if pending else {}
        steps.append(step)

    values = checkpoints[-1].checkpoint["channel_values"] if checkpoints else {}
    return {
        "customer_email": values.get("customer_email"),
        "request_text": values.get("request_text"),
        "steps": steps,
        "pending": pending,
        "outcome": values.get("outcome"),
    }


class RunStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def list_runs(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """Live runs (no eval rows), paused ones first, then newest first."""
        query = f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE source <> 'eval'"
        params: list = []
        if status:
            query += " AND status = %s"
            params.append(status)
        query += " ORDER BY status = 'pending_approval' DESC, created_at DESC LIMIT %s"
        params.append(limit)
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            return [clean_row(row) for row in conn.execute(query, params).fetchall()]

    def get_run(self, run_id: str) -> dict | None:
        query = f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE run_id = %s AND source <> 'eval'"
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            row = conn.execute(query, [run_id]).fetchone()
        return clean_row(row) if row else None

    def trace(self, run_id: str) -> dict:
        with PostgresSaver.from_conn_string(self.conninfo) as saver:
            newest_first = list(saver.list({"configurable": {"thread_id": run_id}}))
        return build_trace(newest_first[::-1])
