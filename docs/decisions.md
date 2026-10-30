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
