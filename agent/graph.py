"""The LangGraph graph and helpers to run, pause and resume one request."""

import asyncio
import uuid

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from agent import nodes
from agent.nodes import Deps, State

RECURSION_LIMIT = 25  # graph steps per invocation; one revision loop needs ~8


def traced(name: str, fn, deps: Deps):
    """Run a node inside a tracing span named after it."""

    async def node(state: State) -> dict:
        observation = deps.llm.tracer.start(state["run_id"], name, input=None)
        try:
            update = await fn(state, deps)
        except GraphInterrupt:
            observation.end(output="waiting for human approval")
            raise
        except Exception as e:
            observation.end(error=f"{type(e).__name__}: {e}")
            raise
        observation.end(output=update)
        return update

    return node


def build_graph(deps: Deps, checkpointer, critic: bool = True):
    """critic=False skips the critic (ablation): tool_agent goes straight to the gate."""
    graph = StateGraph(State)
    names = ["planner", "retriever", "tool_agent", "approval_gate", "commit"]
    if critic:
        names.insert(3, "critic")
    for name in names:
        graph.add_node(name, traced(name, getattr(nodes, name), deps))
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "retriever")
    graph.add_edge("retriever", "tool_agent")
    if critic:
        graph.add_edge("tool_agent", "critic")
        graph.add_conditional_edges("critic", nodes.after_critic, ["tool_agent", "approval_gate"])
    else:
        graph.add_edge("tool_agent", "approval_gate")
    graph.add_edge("approval_gate", "commit")
    graph.add_edge("commit", END)
    return graph.compile(checkpointer=checkpointer)


def config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}


def result(values: dict) -> dict:
    if "__interrupt__" in values:
        return {"status": "pending_approval", "approval_request": values["__interrupt__"][0].value}
    return values["outcome"]


async def start(graph, request: dict, run_id: str | None = None, timeout_s: float = 120) -> dict:
    """Run one request. Returns the outcome, or a pending_approval result to resume later."""
    run_id = run_id or uuid.uuid4().hex
    state = {
        "run_id": run_id,
        "request_id": request["id"],
        "customer_email": request["customer_email"],
        "request_text": request["request_text"],
    }
    async with asyncio.timeout(timeout_s):
        values = await graph.ainvoke(state, config(run_id))
    return {"run_id": run_id, **result(values)}


async def resume(graph, run_id: str, answer: dict, timeout_s: float = 60) -> dict:
    """Continue a paused run with a human decision (see nodes.approval_gate)."""
    async with asyncio.timeout(timeout_s):
        values = await graph.ainvoke(Command(resume=answer), config(run_id))
    return {"run_id": run_id, **result(values)}
