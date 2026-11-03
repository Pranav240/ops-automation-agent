Retrieval on dev (72 requests with relevant policies), query = request text, commit f513c0b, 2026-11-03

| Variant | Hit rate@5 | MRR@5 |
|---|---|---|
| vector | 0.76 [0.65, 0.85] (55/72) | 0.63 |
| fulltext | 0.68 [0.57, 0.78] (49/72) | 0.57 |
| hybrid | 0.79 [0.68, 0.87] (57/72) | 0.59 |
| hybrid_rerank | 0.81 [0.70, 0.88] (58/72) | 0.66 |
| qdrant | 0.76 [0.65, 0.85] (55/72) | 0.63 |
