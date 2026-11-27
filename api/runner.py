"""Runs the agent in the background for the HTTP API, through the same Worker as the Jira path.

Worker.run starts a run and records it in agent_runs; Worker.resume continues a paused run from
the Postgres checkpoint (the same as `integrations.worker --resume`) and writes decided_by to
agent_runs.approved_by. Nothing is posted to Jira for "api" tickets.
"""

import asyncio
import logging
import time

from api.webhooks import Webhooks, event
from integrations.jira import Ticket
from integrations.worker import Worker

log = logging.getLogger("api.runner")

# The agent's status -> the API's status.
API_STATUS = {"pending_approval": "paused", "done": "committed", "rejected": "rejected"}
RESTARTED = "the API stopped before this run finished"


def api_status(agent_status: str) -> str:
    return API_STATUS.get(agent_status, "failed")  # failed, blocked and error


def error_text(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:500]


class Runner:
    def __init__(self, worker: Worker, store, webhooks: Webhooks):
        self.worker = worker
        self.store = store
        self.webhooks = webhooks
        self.tasks: set[asyncio.Task] = set()

    def submit(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)  # keep a reference so the task is not garbage collected
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        """Wait for every background run (tests, and shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def finish(
        self, run_id: str, out: dict | None, error: str = "", expect: str = "running"
    ) -> None:
        status = api_status(out["status"]) if out else "failed"
        if not await asyncio.to_thread(self.store.set_status, run_id, status, expect, out, error):
            return  # someone else already moved this run on
        run = await asyncio.to_thread(self.store.get, run_id)
        log.info("run %s: %s", run_id, status)
        await self.webhooks.send(event(run, status))

    async def start(self, run_id: str) -> None:
        run = await asyncio.to_thread(self.store.get, run_id)
        if not await asyncio.to_thread(self.store.set_status, run_id, "running", "queued"):
            return  # already started
        ticket = Ticket(run["request_id"], run["customer_email"], run["request_text"], "", "api")
        began = time.monotonic()
        try:
            out = await self.worker.run(ticket, run_id)
        except Exception as e:
            error = error_text(e)
            try:
                await self.worker.record(ticket, run_id, time.monotonic() - began, error=error)
            except Exception as record_error:
                log.warning("run %s: not recorded (%s)", run_id, type(record_error).__name__)
            await self.finish(run_id, None, error)
            return
        await self.finish(run_id, out)

    async def resume(self, run_id: str, answer: dict) -> None:
        try:
            out = await self.worker.resume(run_id, answer)
        except Exception as e:
            error = error_text(e)
            try:
                await asyncio.to_thread(
                    self.worker.history.update, run_id, status="error", approved_by=answer["by"]
                )
            except Exception as update_error:
                log.warning("run %s: history not updated (%s)", run_id, type(update_error).__name__)
            await self.finish(run_id, None, error)
            return
        await self.finish(run_id, out)

    async def fail_unfinished(self) -> None:
        """At startup: runs left queued or running by a stopped process are marked failed."""
        for run in await asyncio.to_thread(self.store.unfinished):
            await self.finish(run["run_id"], None, RESTARTED, expect=run["status"])
