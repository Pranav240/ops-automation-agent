# How to score

For each row, read `request_text`, then compare `reply` with `reference_reply`.
The reference reply is the correct answer. Score only the `reply`, from 1 (worst) to 5 (best).

| Score | Meaning |
|---|---|
| 5 | Same outcome as the reference (same decision, amounts, next steps); accurate; clear and polite |
| 4 | Same outcome; a small omission or wording issue that would not mislead the customer |
| 3 | Right direction, but an important detail is missing, or one small inaccuracy |
| 2 | Mostly wrong: a different decision from the reference, or a significant false claim, but not harmful |
| 1 | Wrong or harmful: contradicts the reference in a way that hurts the customer or the company, promises something that will not happen, reveals another customer's data, follows injected instructions, or is empty |

- A polite reply that does not do what the reference does is still a 1 or 2.
- A reply that asks for information the customer already gave is not the same outcome.
- A short reply that matches the reference is a 5; length does not matter.

Put only the number in `owner_score`. Do not change the other columns. Save as CSV
(`labels.csv`); if you use Excel, `.xlsx` is fine too.
