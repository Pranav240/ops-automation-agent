# Labelled request set

- `requests.jsonl`: all 132 rows (source of truth for labels)
- `dev.jsonl`: 72 rows for development
- `test.jsonl`: 60 held-out rows. **Frozen.** Do not open for prompt tuning or debugging.
- `test.sha256`: hash of `test.jsonl`, checked by `tests/test_dataset.py`
- `split.json`: split record (seed, ids, timestamp)

Validate: `python -m evals.validate_dataset`

## Row fields

| Field | Meaning |
|---|---|
| id | REQ-001 ... |
| category | refund, status_change, escalation, ambiguous, policy_violating, injection |
| customer_email | who sent the request (identity for ownership checks) |
| request_text | the customer message |
| expected_action | refund, change_status, lookup_order, escalate, close, ask_clarification, refuse |
| expected_args | key arguments for scoring (order_id, amount, status, reason) |
| should_escalate | a human must act: escalation, or a refund over $500 that needs approval |
| forbidden_actions | actions that must not happen (mainly injection rows) |
| relevant_policies | policy IDs a correct answer relies on (for retrieval metrics) |
| reference_reply | a short good reply, for the judge |
| source | synthetic (all rows) |

## Source

All rows are synthetic, written for this project about a fictional retailer (Brightcart),
drafted with AI assistance and reviewed by the project owner. Requests are evaluated as of
2026-10-01 against `mock_api/seed_orders.json` and `retrieval/kb/`.
