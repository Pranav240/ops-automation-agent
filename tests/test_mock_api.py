import pytest
from fastapi.testclient import TestClient

from mock_api.app import app


@pytest.fixture
def api():
    client = TestClient(app)
    client.post("/reset")
    return client


def test_lookup_order(api):
    r = api.get("/orders/ORD-1001")
    assert r.status_code == 200
    assert r.json()["customer_email"] == "alice.ng@example.com"


def test_lookup_unknown_order_is_404(api):
    assert api.get("/orders/ORD-9999").status_code == 404


def test_refund_updates_order_and_logs_action(api):
    r = api.post(
        "/refunds", json={"order_id": "ORD-1001", "amount": 45.0, "reason": "change_of_mind"}
    )
    assert r.status_code == 200
    assert api.get("/orders/ORD-1001").json()["refunded_amount"] == 45.0
    assert [a["action"] for a in api.get("/actions").json()] == ["refund", "lookup_order"]


def test_full_refund_marks_order_refunded(api):
    api.post(
        "/refunds", json={"order_id": "ORD-1001", "amount": 65.0, "reason": "damaged_or_defective"}
    )
    assert api.get("/orders/ORD-1001").json()["status"] == "refunded"


def test_refund_cannot_exceed_amount_paid(api):
    r = api.post(
        "/refunds", json={"order_id": "ORD-1013", "amount": 200.0, "reason": "change_of_mind"}
    )
    assert r.status_code == 409


def test_refund_on_already_refunded_order_is_rejected(api):
    r = api.post(
        "/refunds", json={"order_id": "ORD-1017", "amount": 30.0, "reason": "change_of_mind"}
    )
    assert r.status_code == 409


def test_refund_over_threshold_needs_approval(api):
    body = {"order_id": "ORD-1011", "amount": 649.0, "reason": "change_of_mind"}
    assert api.post("/refunds", json=body).status_code == 403
    approved = api.post("/refunds", json={**body, "approved_by": "reviewer-1"})
    assert approved.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"order_id": "ORD-1001", "amount": -5, "reason": "change_of_mind"},
        {"order_id": "ORD-1001", "amount": 5, "reason": "because"},
        {"order_id": "1001", "amount": 5, "reason": "change_of_mind"},
        {"order_id": "ORD-1001", "amount": 5, "reason": "change_of_mind", "destination": "btc"},
    ],
)
def test_refund_rejects_invalid_arguments(api, body):
    assert api.post("/refunds", json=body).status_code == 422


def test_cancel_unshipped_order_refunds_total(api):
    r = api.post("/orders/ORD-1022/status", json={"status": "cancelled"})
    assert r.status_code == 200
    order = api.get("/orders/ORD-1022").json()
    assert order["status"] == "cancelled"
    assert order["refunded_amount"] == 560.0


def test_cannot_cancel_shipped_order(api):
    assert api.post("/orders/ORD-1003/status", json={"status": "cancelled"}).status_code == 409


def test_hold_and_release(api):
    assert api.post("/orders/ORD-1036/status", json={"status": "on_hold"}).status_code == 200
    assert api.post("/orders/ORD-1036/status", json={"status": "processing"}).status_code == 200


def test_cannot_set_delivered(api):
    assert api.post("/orders/ORD-1036/status", json={"status": "delivered"}).status_code == 422


def test_escalation_requires_known_reason(api):
    ok = {"order_id": "ORD-1034", "reason": "safety", "summary": "heater smoked"}
    assert api.post("/escalations", json=ok).status_code == 200
    bad = {**ok, "reason": "vip"}
    assert api.post("/escalations", json=bad).status_code == 422


def test_close_ticket_once(api):
    assert api.post("/tickets/T-1/close", json={"resolution": "resolved"}).status_code == 200
    assert api.post("/tickets/T-1/close", json={"resolution": "resolved"}).status_code == 409


def test_reset_restores_seed(api):
    api.post("/orders/ORD-1005/status", json={"status": "cancelled"})
    api.post("/reset")
    assert api.get("/orders/ORD-1005").json()["status"] == "processing"
