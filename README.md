# Ops Automation Agent

Agentic automation for inbound business requests. Work in progress (Phase 0).

## Setup

```bash
uv sync --locked --extra dev   # installs exact versions from uv.lock into .venv
cp .env.example .env   # fill in keys
docker compose up -d   # Postgres + pgvector on port 5433
.venv/Scripts/pre-commit.exe install
```

## Checks

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/smoke_llm.py   # one real Haiku call + one refused call
```

## LLM layer

All LLM calls go through `agent/llm`. Each run has its own budget:
per-request token cap, per-run spend cap, max calls. Every call (and every refusal)
is logged with its cost to Langfuse when Langfuse keys are set.
