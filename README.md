# Ops Automation Agent

Agentic automation for inbound business requests. Work in progress (Phase 4: Jira + text-to-SQL). Dev results: `results/2026-11-03_dev.md`.

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

## Evaluation (dev split only until Phase 8)

```bash
uv run python -m evals.runner --systems B2,B1,A-H --split dev          # results/runs/*.jsonl
uv run python -m evals.runner --systems A-H --no-critic                # ablation
uv run python -m evals.report results/runs/<files> --out results/<name>   # table with 95% CIs
uv run python -m evals.retrieval_eval --out results/<name>             # retrieval variants incl. Qdrant
uv run python -m evals.judge score results/runs/<files>                # Sonnet judge, 30 replies each
uv run python -m evals.ci_gate                                         # CI gate on a fixed dev subset
```

## Jira and run history

```bash
uv run python -m agent.run_history setup                       # run history tables + read-only SQL role
uv run python -m agent.run_history load results/runs/*.jsonl   # import saved dev eval runs
uv run python -m integrations.worker --once                    # handle new Jira requests (needs JIRA_* in .env)
uv run python -m integrations.worker --resume RUN_ID --decision approve --by owner
uv run python -m agent.sql_agent "How many refunds went to approval?"
```

The worker labels each new ticket `ops-agent`, runs the agent with the reporter's email as the
customer, posts the reply as a public comment and moves the ticket on; runs paused for approval
get an internal note instead. The SQL agent writes one SELECT over the `run_history` view, which
is checked with sqlglot and run as a read-only role; every question is logged in `sql_query_log`.

Metric definitions are in `evals/metrics.py` and `evals/preregistration.md`. Qdrant (for the
retrieval comparison only): `docker compose up -d`, then `uv run python -m retrieval.qdrant_store`.

## AWS intake

Requests arrive at a signed, throttled API or from a Jira poll every 5 minutes. Step Functions
hands each one to the worker through SQS, and waits for a human when a run is risky. Failures land
in dead-letter queues. All of it is in `infra/aws`. Steps are in [docs/runbook.md](docs/runbook.md).

```
python -m integrations.submit REQ-T1 ben.okafor@example.com "Where is order ORD-1004?"
AWS_PROFILE=ops-worker python -m integrations.queue_worker --once
```

## Kubernetes

Workers run from `docker/worker.Dockerfile` with the Helm chart in `deploy/helm/ops-agent`. KEDA
scales them on work queue depth, from 0 to 5. A 40-request burst went 0 to 5 and back to 0 on kind
and on EKS: see `results/2026-11-04_kind_scaling.json` and `results/2026-11-04_eks_scaling.json`.
Steps are in [docs/runbook.md](docs/runbook.md).

## Dashboard and security

`dashboard/` has the React + TypeScript app and its API: a request queue, a trace of each run, and
an approve, edit or reject screen. Sign in with a viewer or approver token. Each piece of the system
has its own scoped tokens, database role and IAM role: see [docs/security.md](docs/security.md).
`scripts/injection_test.py` attacks the running system.

## How it works

- `agent/graph.py`: LangGraph graph `planner -> retriever -> tool_agent -> critic -> approval_gate -> commit`, with a Postgres checkpointer so a paused run can be resumed from another process.
- `agent/nodes.py`: the nodes. The tool agent can read orders and propose one write action; writes only run in `commit`, after the critic (one revision allowed) and the approval gate (refunds over $500, low confidence, or unresolved critic issues pause for a human).
- `agent/tools.py`: strict argument models and an async mock-API client with retries.
- `retrieval/`: LlamaIndex ingestion; search is pgvector + Postgres full-text, fused with RRF, then a cross-encoder reranker.
- `agent/baselines.py`: B1 single LLM call and B2 rule-based router.
- `integrations/`: Jira Service Management client and the worker loop.
- `agent/sql_agent.py`, `agent/run_history.py`: text-to-SQL over run history.
- `agent/llm/`: every LLM call goes through here. Each run has a per-request token cap, a per-run spend cap and a max number of calls; every call and graph node is traced in Langfuse with cost and latency.

Langfuse: if your project is in the US region, set `LANGFUSE_BASE_URL=https://us.cloud.langfuse.com`.
