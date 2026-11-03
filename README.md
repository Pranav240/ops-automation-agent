# Ops Automation Agent

Agentic automation for inbound business requests. Work in progress (Phase 2: agent core).

## Setup

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync --locked --extra dev   # installs exact versions from uv.lock into .venv
cp .env.example .env           # fill in keys
docker compose up -d           # Postgres + pgvector on port 5433
uv run pre-commit install
uv run python -m retrieval.ingest   # embed the policy docs into Postgres (downloads ~70 MB model once)
```

## Run

```bash
uv run uvicorn mock_api.app:app --port 8000          # mock internal API, in its own terminal
uv run python -m agent.run REQ-002 REQ-024           # run dev-set requests end to end
uv run python -m agent.run --resume RUN_ID --decision approve --by owner   # resume a paused run
```

Only dev-set IDs can be run; the held-out test split is not read by `agent.run`.

## Checks

```bash
uv run pytest -q
uv run python scripts/smoke_llm.py   # one real Haiku call + one refused call
uv run python -m evals.validate_dataset
```

## How it works

- `agent/graph.py`: LangGraph graph `planner -> retriever -> tool_agent -> critic -> approval_gate -> commit`, with a Postgres checkpointer so a paused run can be resumed from another process.
- `agent/nodes.py`: the nodes. The tool agent can read orders and propose one write action; writes only run in `commit`, after the critic (one revision allowed) and the approval gate (refunds over $500, low confidence, or unresolved critic issues pause for a human).
- `agent/tools.py`: strict argument models and an async mock-API client with retries.
- `retrieval/`: LlamaIndex ingestion; search is pgvector + Postgres full-text, fused with RRF, then a cross-encoder reranker.
- `agent/baselines.py`: B1 single LLM call and B2 rule-based router.
- `agent/llm/`: every LLM call goes through here. Each run has a per-request token cap, a per-run spend cap and a max number of calls; every call and graph node is traced in Langfuse with cost and latency.

Langfuse: if your project is in the US region, set `LANGFUSE_BASE_URL=https://us.cloud.langfuse.com`.
