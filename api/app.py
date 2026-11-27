"""The HTTP API: start a run, read it, decide a paused run. Every call but /health is signed.

POST /runs                     start a run (202, runs in the background, idempotent on request_id)
GET  /runs/{run_id}            status, proposed action, critic notes, reply text and cost
POST /runs/{run_id}/decision   approve | edit | reject a paused run (idempotent on decision_id)
GET  /health

There is one tenant: the mock shop. A request for any other tenant_id gets 403.
Run: python -m api   (see api/__main__.py)
"""

import asyncio
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api import signing
from api.store import NotPaused
from api.webhooks import pause_reason, proposed_action
from dashboard.app import SECURITY_HEADERS, edited_args

ID = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:\-]+$")


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = ID
    tenant_id: str = Field(min_length=1, max_length=200)
    customer_email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    text: str = Field(min_length=1, max_length=10_000)


class EditedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str | None = None  # if given, must be the proposed action: an edit changes args only
    args: dict


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "edit", "reject"]
    edited_action: EditedAction | None = None
    decided_by: str = Field(min_length=1, max_length=200)
    decision_id: str = ID


def view(run: dict) -> dict:
    """What GET /runs/{run_id} shows."""
    result = run.get("result") or {}
    source = result.get("approval_request", result)
    finished_ok = run["status"] in ("paused", "committed")  # paused: a draft, not sent yet
    return {
        "run_id": run["run_id"],
        "request_id": run["request_id"],
        "tenant_id": run["tenant_id"],
        "status": run["status"],
        "proposed_action": proposed_action(result),
        "critic_notes": source.get("critique"),
        "reason_for_pause": pause_reason(result) if run["status"] == "paused" else None,
        "reply_text": source.get("reply") if finished_ok else None,
        "executed": bool(result.get("executed")),
        "decided_by": run.get("decided_by"),
        "cost_usd": run.get("cost_usd"),
        "error": run.get("error"),
        "created_at": run["created_at"].isoformat() if run.get("created_at") else None,
        "updated_at": run["updated_at"].isoformat() if run.get("updated_at") else None,
    }


def create_app(
    store,
    tenant_id: str,
    secret: str,
    runner=None,
    lifespan: AbstractAsyncContextManager | None = None,
) -> FastAPI:
    if not tenant_id or not secret:
        raise ValueError("ALLOWED_TENANT_ID and HTTP_API_HMAC_SECRET must be set")
    app = FastAPI(
        title="Ops agent HTTP API", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=lifespan,
    )  # fmt: skip
    app.state.runner = runner

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    async def signed(request: Request) -> None:
        refused = signing.check(
            secret,
            request.headers.get(signing.TIMESTAMP_HEADER),
            request.headers.get(signing.SIGNATURE_HEADER),
            await request.body(),
        )
        if refused:
            raise HTTPException(401, refused)

    async def get_run(run_id: str) -> dict:
        run = await asyncio.to_thread(store.get, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/runs", status_code=202, dependencies=[Depends(signed)])
    async def start_run(body: RunIn):
        if body.tenant_id != tenant_id:
            raise HTTPException(403, "unknown tenant")
        run, created = await asyncio.to_thread(
            store.create_run,
            uuid.uuid4().hex,
            body.request_id,
            body.tenant_id,
            body.customer_email,
            body.text,
        )
        same = (run["tenant_id"], run["customer_email"], run["request_text"])
        if same != (body.tenant_id, body.customer_email, body.text):
            raise HTTPException(409, "this request_id was already used for a different request")
        if created:
            app.state.runner.submit(app.state.runner.start(run["run_id"]))
        return {"run_id": run["run_id"], "status": run["status"]}

    @app.get("/runs/{run_id}", dependencies=[Depends(signed)])
    async def read_run(run_id: str):
        return view(await get_run(run_id))

    @app.post("/runs/{run_id}/decision", status_code=202, dependencies=[Depends(signed)])
    async def decide(run_id: str, body: DecisionIn):
        run = await get_run(run_id)
        accepted = {"run_id": run_id, "decision_id": body.decision_id, "decision": body.decision}

        async def replay() -> dict:
            earlier = await asyncio.to_thread(store.decision, body.decision_id)
            if earlier["run_id"] != run_id or earlier["decision"] != body.decision:
                raise HTTPException(409, "this decision_id was already used for another decision")
            return {**accepted, "status": (await get_run(run_id))["status"], "duplicate": True}

        if await asyncio.to_thread(store.decision, body.decision_id):
            return await replay()
        if run["status"] != "paused":
            raise HTTPException(409, f"this run is {run['status']}, not paused")
        if body.decision != "edit" and body.edited_action is not None:
            raise HTTPException(422, "only an edit takes edited_action")
        answer = {"decision": body.decision, "by": body.decided_by}
        if body.decision == "edit":
            pending = run["result"]["approval_request"]
            if body.edited_action is None:
                raise HTTPException(422, "an edit needs edited_action")
            if body.edited_action.action not in (None, pending["action"]):
                raise HTTPException(422, "an edit cannot change the action, only its args")
            answer["args"] = edited_args(pending, body.edited_action.args)
        try:
            stored = await asyncio.to_thread(
                store.add_decision,
                body.decision_id,
                run_id,
                body.decision,
                answer.get("args"),
                body.decided_by,
            )
        except NotPaused:
            raise HTTPException(409, "this run is no longer paused") from None
        if not stored:  # the same decision_id arrived at the same moment
            return await replay()
        app.state.runner.submit(app.state.runner.resume(run_id, answer))
        return {**accepted, "status": "running", "duplicate": False}

    return app
