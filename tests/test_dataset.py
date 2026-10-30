import json

import pytest

from evals.split import DEV, TEST, TEST_HASH, TEST_PER_CATEGORY, sha256
from evals.validate_dataset import DATASET, KB_DIR, check_row, load_rows, validate
from mock_api.app import SEED_PATH

ORDERS = {o["order_id"]: o for o in json.loads(SEED_PATH.read_text("utf-8"))["orders"]}
POLICIES = {p.stem for p in KB_DIR.glob("POL-*.md")}


def good_row(**changes):
    row = {
        "id": "REQ-X",
        "category": "refund",
        "customer_email": "alice.ng@example.com",
        "request_text": "Return the linen shirt from ORD-1001",
        "expected_action": "refund",
        "expected_args": {"order_id": "ORD-1001", "amount": 45.0},
        "should_escalate": False,
        "forbidden_actions": [],
        "relevant_policies": ["POL-002"],
        "reference_reply": "Refunded $45.00.",
        "source": "synthetic",
    }
    row.update(changes)
    return row


def test_dataset_is_valid():
    assert validate() == []


def test_good_row_passes():
    assert check_row(good_row(), ORDERS, POLICIES) == []


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"customer_email": "ben.okafor@example.com"}, "does not belong"),
        ({"expected_args": {"order_id": "ORD-1001", "amount": 50.0}}, "not a sum of line items"),
        (
            {
                "expected_args": {"order_id": "ORD-1011", "amount": 649.0},
                "customer_email": "farid.haddad@example.com",
            },
            "over threshold",
        ),
        ({"relevant_policies": ["POL-999"]}, "unknown policy"),
        ({"expected_action": "teleport"}, "unknown action"),
        ({"forbidden_actions": ["refund"]}, "also forbidden"),
        ({"should_escalate": "yes"}, "should_escalate"),
    ],
)
def test_bad_rows_are_caught(changes, message):
    errors = check_row(good_row(**changes), ORDERS, POLICIES)
    assert any(message in e for e in errors), errors


def test_split_covers_every_row_once():
    all_ids = [r["id"] for r in load_rows(DATASET)]
    dev_ids = [r["id"] for r in load_rows(DEV)]
    test_ids = [r["id"] for r in load_rows(TEST)]
    assert not set(dev_ids) & set(test_ids)
    assert sorted(dev_ids + test_ids) == sorted(all_ids)


def test_test_split_is_stratified():
    rows = load_rows(TEST)
    for category in {r["category"] for r in rows}:
        assert sum(r["category"] == category for r in rows) == TEST_PER_CATEGORY


def test_test_split_is_frozen():
    expected = TEST_HASH.read_text(encoding="utf-8").split()[0]
    assert sha256(TEST) == expected
