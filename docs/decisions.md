# Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-10-08 | Dev model `claude-haiku-5-5`, Sonnet = `claude-sonnet-5-5` | Current Haiku and Sonnet; Haiku 5.5 is cheapest for dev |
| 2026-10-08 | Postgres on host port 5433 | 5432 already used on the dev machine |
| 2026-10-19 | All policies and requests synthetic, fictional retailer Brightcart | Avoids copying help-center text; source noted per file |
| 2026-10-19 | Langfuse reads use `/api/public/v2/observations` | Legacy trace API returns 410 for orgs created after 2026-09-16 |
| 2026-10-21 | Budget plan B: add Jev as baseline B3; run Sonnet agent once on test; judge 30 sampled replies per system | Fits the ~$30 target (estimate $20-30) |
| 2026-10-21 | Hermes 4 70B (A-O) optional, only if dev spend < $4 | Extra cost $5-8; open-model comparison is nice to have |
| 2026-10-21 | Judge stays Sonnet only; Sonnet agent replies are scored by the owner by hand | Judge never scores its own model; no extra judge model |
| 2026-10-21 | Cut order if behind: Hermes (A-O), Jev (B3), EKS, text-to-SQL, dashboard polish | Extras go first |
| 2026-10-30 | Add safe rollout to Phase 6: git release tags, images tagged by version + commit, `helm rollback` drill, ConfigMap feature flags (critic on/off, approve-all mode), canary = 1 of 5 workers on the same queue | Close launch-checklist gap (rollout); free |
| 2026-10-30 | Add incident handling: `docs/runbook.md` from Phase 5 (DLQ full, spend cap hit, stuck approval, key rotation); game day + retro in `docs/incidents/` in Phase 6; real pilot issues logged the same way in Phase 8, each becoming a regression test | Close launch-checklist gap (incidents); free |
| 2026-11-03 | Add AWS Lambda (intake), S3 (Terraform state), DynamoDB (idempotency keys, on-demand), CloudWatch logs + alarms (DLQ depth, Lambda errors) to Phase 5 | Name the AWS services explicitly; free tier, about $0-1 |
| 2026-11-03 | Add OpenTelemetry: one trace per request across intake -> queue -> worker -> API, exported over OTLP to Langfuse | End-to-end tracing and real latency; no new service; free |
| 2026-11-03 | Async Python for API endpoints, workers and tool calls (Phases 2, 5, 6) | Workers wait on LLM and HTTP calls; free |
| 2026-11-03 | Phase 7 dashboard in React + TypeScript | Type safety; free |
| 2026-11-03 | Add Qdrant (Docker) as an extra retrieval variant, dev set only; pgvector stays the main store | Compare vector stores; free |
| 2026-11-03 | Embeddings `BAAI/bge-small-en-v1.5` and reranker `Xenova/ms-marco-MiniLM-L-6-v2`, both local via fastembed (ONNX, CPU) | Free, small (~70-80 MB each), no extra API or service |
| 2026-11-03 | LlamaIndex used for ingestion (loading, splitting, embedding); search SQL, RRF and reranking are our own code | Keeps each retrieval variant explicit for the Phase 3 ablations |
| 2026-11-03 | No forced `tool_choice`; JSON comes from structured outputs (`output_config.format`) and tools are `strict` | Sonnet 5.5 rejects forced tool use, and A-S must run the same code as A-H |
| 2026-11-03 | Write actions are proposed by the tool agent and executed only in the commit node; one write per request; orders are checked against the requester's email in code | Approval gate and owner checks cannot be skipped by the model |
| 2026-11-03 | Critic revision continues the tool agent's conversation instead of restarting it | First real run needed 9 of the 10 allowed LLM calls; continuing saves the repeated lookups |
| 2026-11-03 | Qdrant retrieval variant moves to Phase 3 with the other retrieval ablations | It is only used for the dev-set comparison |
| 2026-11-03 | Reranker stays `ms-marco-MiniLM-L-6-v2`; L-12, bge-reranker-base and jina were within noise on dev (hit@5 0.78-0.83) | Smallest and fastest of the equal options |
| 2026-11-03 | A declined model call (`stop_reason: refusal`, empty content) is handled as a refusal in the planner, tool agent, critic and B1 | REQ-125 crashed the run on an empty reply |
| 2026-11-03 | Planner search query names the policy topics needed instead of repeating the request | Most dev retrieval misses were POL-024 (clarifying questions); agent hit@5 went from 0.83 to 0.97 |
| 2026-11-03 | B1 output schema uses enums for status and escalation reason | Free-text reasons scored 0/11 on reason accuracy, which measured the schema, not the model |
| 2026-11-03 | CI eval gate runs on pull requests only: 12 dev rows (2 per category) plus the live regression cases, about $0.04 | Real LLM calls cost money; pushes run lint and unit tests only |
| 2026-11-03 | Development stopped at f513c0b (stopping rule 1: all targets met on dev at 9c58580 and f513c0b); tagged `agent-frozen-v1` | Preregistered stopping rule; REQ-004 kept as a known flaky regression case instead of tuning further |
| 2026-11-03 | Known issue kept in the frozen agent: on refunds over $500 the agent sometimes says the refund is pending approval without calling `issue_refund` (REQ-004, REQ-013; about 1 in 3). REQ-013 also marked known flaky. Fix (one prompt rule + 2 dev runs, about $1) deferred for budget | Found by the CI regression tests on PR #3; dev averages hid it (3 of 72 dev rows are refunds over $500) |
