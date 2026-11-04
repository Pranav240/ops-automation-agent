# Access and injection defences

Each piece holds only what it uses. Automated checks keep this true: `tests/test_security.py` and
`tests/test_mock_api.py` run in CI, and `scripts/injection_test.py` tries the forbidden calls on the
running system.

## Who can do what

| Piece | Mock API | Postgres role | AWS | Other secrets |
|---|---|---|---|---|
| Planner, critic | none | none directly | none | none |
| Tool agent | read token: order lookups only, and only code calls it | none directly | none | none |
| Commit node | write token: the four actions, no lookups, reset or log | none directly | none | none |
| Worker process (all of the above) | read and write tokens | `ops_worker`: read, insert and update runs and checkpoints, read policies | SQS receive and delete, send task success. `sqs:GetQueueAttributes` for KEDA on kind only | LLM key, Langfuse keys, Jira token |
| Text-to-SQL agent | none | `sql_reader`: read-only `run_history` view, no customer emails | none | LLM key |
| Dashboard | none | `ops_dashboard`: read-only runs and checkpoints | read an approval token, send task success | two sign-in tokens |
| KEDA | none | none | on EKS `sqs:GetQueueAttributes`; on kind the worker's keys | none |
| Mock API | checks tokens | none | none | three token values |
| Setup job | none | database owner, then exits | none | database passwords |
| Test scripts | admin token: reset and the action log | none | none | run on the owner's machine |

The dashboard cannot execute anything. A decision goes to Step Functions and the worker carries it
out. It also has no LLM key, so a stolen dashboard token cannot spend LLM credit by itself.

## Dashboard sign-in

- Two bearer tokens: `DASHBOARD_VIEWER_TOKEN` reads and `DASHBOARD_APPROVER_TOKEN` also decides.
- The name recorded for a decision comes from the token. A request that names a decider is refused.
- An unset token opens nothing. A missing token stops the chart from rendering.
- The token stays in `sessionStorage` for that tab, and is sent as a header, so there is no cookie
  for another site to use.
- There is no public address. Reach it with `kubectl port-forward`. Plain HTTP is fine only through
  that tunnel; any wider deployment needs TLS first.
- A person can edit the amount, reason, status or text of a proposal. They cannot move it to another
  order, and the arguments are checked before they reach the worker.

## Injection defences, in order

1. Customer text is marked untrusted in every prompt.
2. The model only proposes writes. Tool arguments are strict, so it cannot set `approved_by`.
3. Code checks that an order belongs to the requesting customer, at lookup and again at commit.
4. The critic reviews the decision.
5. Code pauses refunds over $500, low confidence and unresolved critic issues for a human.
6. The mock API refuses a refund over $500 with no `approved_by`.
7. The dashboard shows customer and model text as plain text, with a strict Content Security Policy.

## Known limits

- Tokens are static and scoped per piece, not per run. Rotate by changing `.env` and redeploying.
- The read and write tokens sit in one worker process. The split stops a bug in the lookup path from
  writing; it does not protect against code running inside the worker.
- `/health` is open on the dashboard and mock API, and returns only `{"ok": true}`.
- Pods in the cluster can reach each other, because no NetworkPolicy is set. Tokens and database
  roles are what stop them.
- Only the customer request text is tested as an attack path. Policy documents and order records are
  treated as trusted, so a poisoned policy document is not covered.
