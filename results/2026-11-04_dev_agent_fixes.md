| Metric | A-H@f513c0b | A-H@25ee42c |
|---|---|---|
| Model | claude-haiku-5-5 | claude-haiku-5-5 |
| Commit | f513c0b | 25ee42c |
| Runs x requests | 3 x 72 | 2 x 72 |
| Errors (first run) | 0 | 0 |
| Correct action rate | 0.97 ± 0.01 [0.93, 1.00] (71/72) | 0.94 ± 0.03 [0.83, 0.96] (66/72) |
| Wrong action rate | 0.00 ± 0.00 [0.00, 0.05] (0/72) | 0.00 ± 0.00 [0.00, 0.05] (0/72) |
| Escalation recall | 0.96 ± 0.04 [0.80, 1.00] (15/15) | 1.00 ± 0.00 [0.80, 1.00] (15/15) |
| Escalation precision | 1.00 ± 0.00 [0.80, 1.00] (15/15) | 0.97 ± 0.04 [0.72, 0.99] (15/16) |
| Escalation reason accuracy | 1.00 ± 0.00 [0.76, 1.00] (12/12) | 1.00 ± 0.00 [0.74, 1.00] (11/11) |
| Injection resistance | 1.00 ± 0.00 [0.76, 1.00] (12/12) | 1.00 ± 0.00 [0.76, 1.00] (12/12) |
| Groundedness (cited in retrieved) | 1.00 ± 0.01 [0.92, 1.00] (70/71) | 0.99 ± 0.02 [0.95, 1.00] (69/69) |
| Replies with no citation | 0.02 ± 0.01 [0.00, 0.07] (1/72) | 0.03 ± 0.01 [0.01, 0.12] (3/72) |
| Retrieval hit@5 (agent query) | 0.97 ± 0.00 [0.90, 0.99] (70/72) | 0.97 ± 0.00 [0.90, 0.99] (70/72) |
| Retrieval MRR@5 (agent query) | 0.88 | 0.88 |
| Owner-mismatch actions | 0 | 0 |
| Cost per request (USD) | 0.00172 | 0.00178 |
| Latency p50 / p95 (s) | 11.7 / 19.8 | 12.8 / 24.1 |

Correct / wrong action rate by category (first run):

| Category | A-H@f513c0b | A-H@25ee42c |
|---|---|---|
| ambiguous | 1.00 / 0.00 (n=12) | 1.00 / 0.00 (n=12) |
| escalation | 1.00 / 0.00 (n=12) | 0.92 / 0.00 (n=12) |
| injection | 1.00 / 0.00 (n=12) | 0.92 / 0.00 (n=12) |
| policy_violating | 0.92 / 0.00 (n=12) | 0.92 / 0.00 (n=12) |
| refund | 1.00 / 0.00 (n=12) | 0.92 / 0.00 (n=12) |
| status_change | 1.00 / 0.00 (n=12) | 0.83 / 0.00 (n=12) |

A-H@25ee42c - A-H@f513c0b, correct rate: -0.069 [-0.125, -0.014] (n=72, significant)
A-H@25ee42c - A-H@f513c0b, wrong rate: +0.000 [+0.000, +0.000] (n=72, not distinguishable)
