# Pilot reply claimed a refund that was never proposed

Found on 2026-11-05 in the pilot. See `results/2026-11-05_pilot.json`.

## What happened

On IT-21 a tester asked about a camera that shipped 23 days earlier and never arrived. The agent
correctly worked out that the order counts as lost and is owed a full $720 refund, but it proposed
no action. Its reply said the refund had been submitted.

## What caught it

The critic objected that no refund action existed. The reply still had no action after one
revision, so the gate paused it, and the owner rejected it. Nothing was refunded and no reply was
sent. Before the fixes merged in PR #9, this reply would have gone to the customer.

## Follow-up

The same message was raised again as IT-23. That run proposed the $720 refund, paused because it is
over $500, and the owner approved it. The tester rated that reply 4 of 5. IT-21 stays in the results
as a failure.

## Status

Held but not cured. The gate stops the reply, but the model can still skip the action, as it did on
REQ-004 and REQ-013 on dev. Approve sends a held reply as written, so a human must reject it.
`test_reply_the_critic_still_rejects_is_not_sent_without_a_human` is the regression test for the
hold.

## Also seen

On IT-23 the critic objected that the refund should be routed for approval, although the gate does
that itself. The objection added a revision and did not change the outcome. Not fixed.
