# Critic cannot see failed lookups

Found on 2026-11-04 in the owner trial. See `results/2026-11-04_owner_trial.json`.

## What happened

On IT-6, IT-8, IT-9, IT-12 and IT-13 the order was not found for that customer. The reply said so,
which was true, and the critic objected that no lookup had happened. The agent revised the reply,
and the gate passed the run because it took no action.

## Cause

`state["orders"]` holds only orders that belong to the customer (`agent/nodes.py`). The critic is
told these are the orders the agent looked up, so a failed lookup looks like no lookup.

## Effect

- These 5 runs used 7 LLM calls each, against 3 to 5 for the others.
- They cost $0.00297 to $0.00328 each. IT-9 took 46.4 s, above the 30 s p95 target.
- The customers still got true replies. No wrong action was taken.
- The agent also repeated each failed lookup once, by retrying the same ID or a bare number.
- REQ-120 on the test set shows the same pattern. Its critic objection was probably false too.

## Why it matters for the next fix

The gate lets a run with no action pass when the critic objects, which is how the false hand-off on
REQ-049 and REQ-054 got through. Tightening the gate alone would now block these true replies, so
the critic must see failed lookups first.

## Status

Not fixed. The agent stays frozen at `agent-frozen-v1` for the reported numbers. When it is fixed,
add a regression test with the fix: a not-found lookup must reach the critic as a failed lookup.
