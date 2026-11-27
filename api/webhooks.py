"""Signed webhooks to GovernedDesk when a run pauses or ends.

Each event is tried 3 times with backoff and every attempt is logged (event id, type, run id and
the HTTP status or error type only: no body, headers or secrets). Sending never raises, so a down
webhook receiver cannot block or fail a run.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

import httpx

from api.signing import signed_headers

log = logging.getLogger("api.webhooks")

TRIES = 3
BACKOFF_S = (1.0, 4.0)  # wait before the 2nd and 3rd tries
EVENTS = {"paused", "committed", "rejected", "failed"}


def proposed_action(result: dict | None) -> dict | None:
    """The action the agent proposed (or took), with its arguments and reply."""
    if not result:
        return None
    source = result.get("approval_request", result)
    if "reply" not in source:
        return None
    return {"action": source.get("action"), "args": source.get("args"), "reply": source["reply"]}


def pause_reason(result: dict | None) -> str | None:
    request = (result or {}).get("approval_request")
    return "; ".join(request["reasons"]) if request else None


def event(run: dict, status: str) -> dict:
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": f"run.{status}",
        "run_id": run["run_id"],
        "request_id": run["request_id"],
        "tenant_id": run["tenant_id"],
        "status": status,
        "proposed_action": proposed_action(run.get("result")),
        "reason_for_pause": pause_reason(run.get("result")) if status == "paused" else None,
        "created_at": datetime.now(UTC).isoformat(),
    }


class Webhooks:
    def __init__(
        self,
        url: str,
        secret: str,
        client: httpx.AsyncClient | None = None,
        backoff_s: tuple[float, ...] = BACKOFF_S,
    ):
        self.url = url
        self.secret = secret
        self.client = client or httpx.AsyncClient(timeout=10)
        self.backoff_s = backoff_s

    async def send(self, payload: dict) -> bool:
        """POST the event. True if the receiver answered 2xx within 3 tries."""
        name = f"{payload['event_type']} {payload['event_id']} run {payload['run_id']}"
        if not self.url:
            log.info("webhook %s: WEBHOOK_URL is not set, not sent", name)
            return False
        try:
            body = json.dumps(payload, separators=(",", ":")).encode()
            for attempt in range(1, TRIES + 1):
                try:
                    headers = {"Content-Type": "application/json"}
                    headers.update(signed_headers(self.secret, body))
                    response = await self.client.post(self.url, content=body, headers=headers)
                    outcome, ok = f"HTTP {response.status_code}", response.is_success
                except httpx.HTTPError as e:
                    outcome, ok = type(e).__name__, False
                log.info("webhook %s attempt %d/%d: %s", name, attempt, TRIES, outcome)
                if ok:
                    return True
                if attempt < TRIES:
                    await asyncio.sleep(self.backoff_s[attempt - 1])
            log.warning("webhook %s: gave up after %d tries", name, TRIES)
        except Exception as e:  # never let a webhook problem reach the run
            log.warning("webhook %s: not sent (%s)", name, type(e).__name__)
        return False

    async def aclose(self) -> None:
        await self.client.aclose()
