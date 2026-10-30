"""Check the labelled request set for schema errors and labels that contradict the seed data.

Usage: python -m evals.validate_dataset [path/to/requests.jsonl]
"""

import json
import sys
from itertools import combinations
from pathlib import Path

from mock_api.app import APPROVAL_THRESHOLD, SEED_PATH, TRANSITIONS, EscalationReason

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "dataset" / "requests.jsonl"
KB_DIR = ROOT / "retrieval" / "kb"

CATEGORIES = {"refund", "status_change", "escalation", "ambiguous", "policy_violating", "injection"}
WRITE_ACTIONS = {"refund", "change_status", "escalate", "close"}
ACTIONS = WRITE_ACTIONS | {"lookup_order", "ask_clarification", "refuse"}
ARG_KEYS = {
    "refund": {"order_id", "amount"},
    "change_status": {"order_id", "status"},
    "escalate": {"order_id", "reason"},
    "lookup_order": {"order_id"},
}
FIELDS = {
    "id": str,
    "category": str,
    "customer_email": str,
    "request_text": str,
    "expected_action": str,
    "expected_args": dict,
    "should_escalate": bool,
    "forbidden_actions": list,
    "relevant_policies": list,
    "reference_reply": str,
    "source": str,
}
MIN_PER_CATEGORY = 15


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def refundable_amounts(order: dict) -> set[float]:
    """Every amount you get by refunding a subset of the order's line items."""
    lines = [round(i["price"] * i["qty"], 2) for i in order["items"]]
    amounts = set()
    for n in range(1, len(lines) + 1):
        for combo in combinations(lines, n):
            amounts.add(round(sum(combo), 2))
    return amounts


def check_row(row: dict, orders: dict, policy_ids: set) -> list[str]:
    errors = []
    for field, kind in FIELDS.items():
        if not isinstance(row.get(field), kind):
            errors.append(f"field {field!r} missing or not {kind.__name__}")
    if errors:
        return errors

    action, args = row["expected_action"], row["expected_args"]
    if row["category"] not in CATEGORIES:
        errors.append(f"unknown category {row['category']!r}")
    if row["source"] not in {"public", "synthetic"}:
        errors.append(f"unknown source {row['source']!r}")
    if not row["request_text"].strip() or not row["reference_reply"].strip():
        errors.append("empty request_text or reference_reply")
    if row["customer_email"] not in {o["customer_email"] for o in orders.values()}:
        errors.append(f"unknown customer {row['customer_email']}")
    if not row["relevant_policies"]:
        errors.append("no relevant_policies")
    for pid in row["relevant_policies"]:
        if pid not in policy_ids:
            errors.append(f"unknown policy {pid}")
    for forbidden in row["forbidden_actions"]:
        if forbidden not in ACTIONS:
            errors.append(f"unknown forbidden action {forbidden!r}")
    if action in row["forbidden_actions"]:
        errors.append("expected_action is also forbidden")

    if action not in ACTIONS:
        return errors + [f"unknown action {action!r}"]
    if set(args) != ARG_KEYS.get(action, set()):
        return errors + [f"args {sorted(args)} != {sorted(ARG_KEYS.get(action, set()))}"]

    order = None
    if args.get("order_id"):
        order = orders.get(args["order_id"])
        if order is None:
            return errors + [f"unknown order {args['order_id']}"]
        if order["customer_email"] != row["customer_email"]:
            errors.append(f"{args['order_id']} does not belong to {row['customer_email']}")

    if action == "refund":
        refundable = round(order["total"] - order["refunded_amount"], 2)
        if args["amount"] > refundable:
            errors.append(f"refund {args['amount']} > refundable {refundable}")
        if args["amount"] not in refundable_amounts(order):
            errors.append(f"refund {args['amount']} is not a sum of line items")
        if (args["amount"] > APPROVAL_THRESHOLD) != row["should_escalate"]:
            errors.append("should_escalate must be true exactly when refund is over threshold")
    elif action == "change_status":
        if args["status"] not in TRANSITIONS.get(order["status"], set()):
            errors.append(f"cannot move {args['order_id']} from {order['status']}")
    elif action == "escalate":
        if args["reason"] not in EscalationReason.__args__:
            errors.append(f"unknown escalation reason {args['reason']!r}")
        if not row["should_escalate"]:
            errors.append("escalate rows must have should_escalate true")
    elif row["should_escalate"]:
        errors.append(f"should_escalate true but action is {action}")
    return errors


def validate(path: Path = DATASET) -> list[str]:
    rows = load_rows(path)
    orders = {o["order_id"]: o for o in json.loads(SEED_PATH.read_text("utf-8"))["orders"]}
    policy_ids = {p.stem for p in KB_DIR.glob("POL-*.md")}

    errors = []
    seen = set()
    for row in rows:
        rid = row.get("id", "?")
        if rid in seen:
            errors.append(f"{rid}: duplicate id")
        seen.add(rid)
        errors += [f"{rid}: {e}" for e in check_row(row, orders, policy_ids)]

    counts = {c: sum(r.get("category") == c for r in rows) for c in CATEGORIES}
    for category, n in counts.items():
        if n < MIN_PER_CATEGORY:
            errors.append(f"category {category} has {n} rows, need >= {MIN_PER_CATEGORY}")
    return errors


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DATASET
    problems = validate(path)
    for p in problems:
        print(p)
    print(f"{len(load_rows(path))} rows, {len(problems)} problems")
    sys.exit(1 if problems else 0)
