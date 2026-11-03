"""Tools the agent can call, their strict argument models, and an async client for the mock API.

lookup_order is a read and runs immediately. The other tools are writes: the tool agent only
proposes them; the commit node executes them after the critic and the approval gate.
"""

import asyncio
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ORDER_ID = r"^ORD-\d{4}$"
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


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LookupArgs(Args):
    order_id: str = Field(pattern=ORDER_ID)


class RefundArgs(Args):
    order_id: str = Field(pattern=ORDER_ID)
    amount: float = Field(gt=0)
    reason: Literal["change_of_mind", "damaged_or_defective", "lost"]


class StatusArgs(Args):
    order_id: str = Field(pattern=ORDER_ID)
    status: Literal["cancelled", "on_hold", "processing"]


class EscalateArgs(Args):
    order_id: str | None = Field(default=None, pattern=ORDER_ID)
    reason: EscalationReason
    summary: str = Field(min_length=1, max_length=500)


class CloseArgs(Args):
    resolution: str = Field(min_length=1, max_length=500)


# tool name -> (action label used in the dataset, argument model)
WRITE_TOOLS: dict[str, tuple[str, type[Args]]] = {
    "issue_refund": ("refund", RefundArgs),
    "change_order_status": ("change_status", StatusArgs),
    "escalate": ("escalate", EscalateArgs),
    "close_ticket": ("close", CloseArgs),
}
ACTION_ARGS = {action: model for action, model in WRITE_TOOLS.values()}


def validate_args(action: str, args: dict) -> dict:
    """Validate write-action arguments; raises ValueError with a short message."""
    try:
        return ACTION_ARGS[action].model_validate(args).model_dump()
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise ValueError(problems) from None


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


_ORDER = {"type": "string", "description": "Order ID like ORD-1001"}
TOOL_SCHEMAS = [
    _tool(
        "lookup_order",
        "Look up one of the customer's orders. Read only; runs immediately.",
        {"order_id": _ORDER},
    ),
    _tool(
        "issue_refund",
        "Propose a refund to the original payment method. Amount in USD.",
        {
            "order_id": _ORDER,
            "amount": {"type": "number"},
            "reason": {
                "type": "string",
                "enum": ["change_of_mind", "damaged_or_defective", "lost"],
            },
        },
    ),
    _tool(
        "change_order_status",
        "Propose a status change: cancelled, on_hold or processing.",
        {
            "order_id": _ORDER,
            "status": {"type": "string", "enum": ["cancelled", "on_hold", "processing"]},
        },
    ),
    _tool(
        "escalate",
        "Propose handing the request to a human team. order_id may be null.",
        {
            "order_id": {"anyOf": [_ORDER, {"type": "null"}]},
            "reason": {"type": "string", "enum": list(EscalationReason.__args__)},
            "summary": {"type": "string", "description": "One or two sentences for the human."},
        },
    ),
    _tool(
        "close_ticket",
        "Propose closing this ticket because nothing else is needed.",
        {"resolution": {"type": "string"}},
    ),
]


@dataclass
class ApiResult:
    ok: bool
    status: int
    body: dict | list | str


class MockApi:
    """Async client. GETs retry on connection errors and 5xx; POSTs retry only when the
    connection failed before the request was sent, so a write is never sent twice."""

    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        retries: int = 3,
        backoff_s: float = 0.5,
    ):
        self.client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout_s)
        self.retries = retries
        self.backoff_s = backoff_s

    async def _request(self, method: str, path: str, json: dict | None = None) -> ApiResult:
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                response = await self.client.request(method, path, json=json)
            except httpx.ConnectError:
                if last:
                    raise
            except httpx.TransportError:
                if method != "GET" or last:
                    raise
            else:
                if response.status_code < 500 or method != "GET" or last:
                    try:
                        body = response.json()
                    except ValueError:
                        body = response.text
                    if isinstance(body, dict) and "detail" in body and not response.is_success:
                        body = body["detail"]
                    return ApiResult(response.is_success, response.status_code, body)
            await asyncio.sleep(self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")

    async def get_order(self, order_id: str) -> ApiResult:
        return await self._request("GET", f"/orders/{order_id}")

    async def execute(self, action: str, args: dict, ticket_id: str, approved_by: str | None):
        if action == "refund":
            return await self._request("POST", "/refunds", {**args, "approved_by": approved_by})
        if action == "change_status":
            return await self._request(
                "POST", f"/orders/{args['order_id']}/status", {"status": args["status"]}
            )
        if action == "escalate":
            return await self._request("POST", "/escalations", args)
        if action == "close":
            return await self._request("POST", f"/tickets/{ticket_id}/close", args)
        raise ValueError(f"not a write action: {action}")

    async def aclose(self):
        await self.client.aclose()
