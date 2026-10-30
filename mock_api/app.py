"""Mock internal API for Brightcart (fictional). In-memory state seeded from seed_orders.json.

Actions: lookup order, refund, change status, escalate, close ticket.
The API enforces hard invariants only (amount limits, status transitions, approval over $500).
Policy decisions (return windows, identity checks) are the agent's job.
"""

import copy
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

SEED_PATH = Path(__file__).parent / "seed_orders.json"
APPROVAL_THRESHOLD = 500.00
ORDER_ID = r"^ORD-\d{4}$"

# Status changes an agent may make: current status -> allowed new statuses.
TRANSITIONS = {
    "placed": {"cancelled", "on_hold"},
    "processing": {"cancelled", "on_hold"},
    "on_hold": {"processing", "cancelled"},
}

EscalationReason = Literal[
    "safety",
    "legal",
    "privacy",
    "security",
    "address_change",
    "warranty",
    "staff_complaint",
    "human_request",
    "media",
    "other",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefundRequest(Strict):
    order_id: str = Field(pattern=ORDER_ID)
    amount: float = Field(gt=0)
    reason: Literal["change_of_mind", "damaged_or_defective", "lost"]
    approved_by: str | None = None


class StatusChange(Strict):
    status: Literal["cancelled", "on_hold", "processing"]


class EscalationRequest(Strict):
    order_id: str | None = Field(default=None, pattern=ORDER_ID)
    reason: EscalationReason
    summary: str = Field(min_length=1, max_length=500)


class CloseRequest(Strict):
    resolution: str = Field(min_length=1, max_length=500)


class Store:
    def __init__(self):
        self.reset()

    def reset(self):
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        self.orders = {o["order_id"]: copy.deepcopy(o) for o in seed["orders"]}
        self.actions: list[dict] = []
        self.closed_tickets: set[str] = set()

    def log(self, action: str, **details) -> dict:
        entry = {"id": len(self.actions) + 1, "action": action, **details}
        self.actions.append(entry)
        return entry

    def order(self, order_id: str) -> dict:
        if order_id not in self.orders:
            raise HTTPException(404, f"order {order_id} not found")
        return self.orders[order_id]


store = Store()
app = FastAPI(title="Brightcart mock internal API")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/reset")
def reset():
    store.reset()
    return {"ok": True}


@app.get("/orders/{order_id}")
def lookup_order(order_id: str):
    order = store.order(order_id)
    store.log("lookup_order", order_id=order_id)
    return order


@app.post("/refunds")
def refund(req: RefundRequest):
    order = store.order(req.order_id)
    amount = round(req.amount, 2)
    if order["status"] in ("cancelled", "refunded"):
        raise HTTPException(409, f"order is {order['status']}")
    refundable = round(order["total"] - order["refunded_amount"], 2)
    if amount > refundable:
        raise HTTPException(409, f"amount {amount:.2f} exceeds refundable {refundable:.2f}")
    if amount > APPROVAL_THRESHOLD and not req.approved_by:
        raise HTTPException(403, f"refunds over {APPROVAL_THRESHOLD:.2f} need human approval")
    order["refunded_amount"] = round(order["refunded_amount"] + amount, 2)
    if order["refunded_amount"] == order["total"]:
        order["status"] = "refunded"
    return store.log(
        "refund",
        order_id=req.order_id,
        amount=amount,
        reason=req.reason,
        approved_by=req.approved_by,
    )


@app.post("/orders/{order_id}/status")
def change_status(order_id: str, req: StatusChange):
    order = store.order(order_id)
    allowed = TRANSITIONS.get(order["status"], set())
    if req.status not in allowed:
        raise HTTPException(409, f"cannot change status from {order['status']} to {req.status}")
    previous = order["status"]
    order["status"] = req.status
    if req.status == "cancelled":
        order["refunded_amount"] = order["total"]
    return store.log("change_status", order_id=order_id, previous=previous, status=req.status)


@app.post("/escalations")
def escalate(req: EscalationRequest):
    if req.order_id:
        store.order(req.order_id)
    return store.log("escalate", order_id=req.order_id, reason=req.reason, summary=req.summary)


@app.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: str, req: CloseRequest):
    if ticket_id in store.closed_tickets:
        raise HTTPException(409, f"ticket {ticket_id} already closed")
    store.closed_tickets.add(ticket_id)
    return store.log("close", ticket_id=ticket_id, resolution=req.resolution)


@app.get("/actions")
def actions():
    return store.actions
