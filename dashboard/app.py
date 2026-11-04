"""Dashboard API, which also serves the built React app.

Two bearer tokens: a viewer reads, an approver reads and decides. The decider's name comes from
the token, never from the request. A decision goes to Step Functions and the worker does the rest,
so this service holds no LLM key and no mock API token, and cannot execute anything.

Run: uvicorn dashboard.app:build --factory --port 8080 --env-file .env
"""

import hmac
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import boto3
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from agent.config import db_url
from agent.tools import validate_args
from dashboard.store import RunStore, trace_id
from integrations.approval import NotWaiting, approve

APPROVER_NAME = "dashboard-approver"
WEB_BUILD = Path(__file__).parent / "web" / "dist"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")  # so a request cannot say who decided

    decision: Literal["approve", "reject", "edit"]
    args: dict | None = None


def edited_args(pending: dict, changes: dict | None) -> dict:
    """The proposal with the human's changes. An edit may not move the action to another order."""
    if not changes:
        raise HTTPException(422, "an edit needs args")
    args = {**pending["args"], **changes}
    if args.get("order_id") != pending["args"].get("order_id"):
        raise HTTPException(422, "an edit cannot change the order")
    try:
        return validate_args(pending["action"], args)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def create_app(
    store,
    send_decision: Callable[[str, dict], None],
    tokens: dict[str, str],
    web_build: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Ops dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def caller(authorization: str = Header(default="")) -> str:
        presented = authorization.removeprefix("Bearer ").encode()
        for scope, token in tokens.items():
            if token and hmac.compare_digest(presented, token.encode()):
                return scope
        raise HTTPException(401, "missing or unknown token")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/me")
    def me(scope: str = Depends(caller)):
        return {"scope": scope}

    @app.get("/api/runs")
    def runs(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        scope: str = Depends(caller),
    ):
        return store.list_runs(status, limit)

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, scope: str = Depends(caller)):
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "run not found")
        return {"run": row, "trace_id": trace_id(run_id), **store.trace(run_id)}

    @app.post("/api/runs/{run_id}/decision", status_code=202)
    def decide(run_id: str, body: Decision, scope: str = Depends(caller)):
        if scope != "approver":
            raise HTTPException(403, "this token cannot decide")
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "run not found")
        pending = store.trace(run_id)["pending"]
        if pending is None:
            raise HTTPException(409, "this run is not waiting for a decision")
        if body.decision != "edit" and body.args is not None:
            raise HTTPException(422, "only an edit takes args")
        answer = {"decision": body.decision, "by": APPROVER_NAME}
        if body.decision == "edit":
            answer["args"] = edited_args(pending, body.args)
        try:
            send_decision(row["request_id"], answer)
        except NotWaiting as e:
            raise HTTPException(409, str(e)) from None
        return {"sent": body.decision}

    if web_build and web_build.exists():
        app.mount("/", StaticFiles(directory=web_build, html=True))
    return app


def build() -> FastAPI:
    table = boto3.resource("dynamodb").Table(os.environ["OPS_RUNS_TABLE"])
    sfn = boto3.client("stepfunctions")
    tokens = {
        "viewer": os.getenv("DASHBOARD_VIEWER_TOKEN", ""),
        "approver": os.getenv("DASHBOARD_APPROVER_TOKEN", ""),
    }
    return create_app(
        RunStore(db_url()),
        lambda request_id, answer: approve(table, sfn, request_id, answer),
        tokens,
        WEB_BUILD,
    )
