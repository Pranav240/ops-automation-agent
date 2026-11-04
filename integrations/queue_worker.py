"""Queue worker: runs the agent for messages from the SQS work queue.

Step Functions puts a message with a task token on the queue and waits. The worker runs the
agent, then returns the result with that token. A run that needs approval comes back as
pending_approval; the state machine then waits until --approve sends the human decision, and
puts a resume message on the queue.

A message that fails stays on the queue, is retried, and lands in the work DLQ after 3 tries.

Needs Postgres, ingested policies, the mock API, and OPS_WORK_QUEUE_URL and OPS_RUNS_TABLE in .env.
AWS access comes from the worker role: AWS_PROFILE=ops-worker (see docs/runbook.md).

SIGTERM finishes the current message, then exits, so scaling down never cuts a run short. Every
poll touches a heartbeat file, which the Kubernetes probes read.

Usage:
  python -m integrations.queue_worker                # keep polling
  python -m integrations.queue_worker --once         # one long poll, then exit
  python -m integrations.queue_worker --setup        # create the checkpoint tables, then exit
  python -m integrations.queue_worker --approve REQUEST_ID --decision approve --by owner
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import boto3
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.config import db_url, git_state, mock_api_url
from agent.graph import build_graph
from agent.llm import LLMClient
from agent.nodes import Deps
from agent.run_history import RunHistory
from agent.tools import MockApi
from integrations.jira import JiraClient, Ticket
from integrations.otel import context_from, make_provider, to_ns, trace_http
from integrations.worker import Worker
from retrieval.search import HybridRetriever

WAIT_S = 20
HEARTBEAT = Path(tempfile.gettempdir()) / "ops-worker-heartbeat"


def to_ticket(request: dict) -> Ticket:
    key = request.get("ticket_key")
    return Ticket(
        key=key or request["id"],
        customer_email=request["customer_email"],
        summary="",
        description=request["request_text"],
        source="jira" if key else "api",
    )


def summary(out: dict) -> dict:
    """What Step Functions keeps; the rest of the run is in Postgres and Langfuse."""
    return {"status": out["status"], "run_id": out["run_id"], "action": out.get("action")}


class QueueWorker:
    def __init__(self, worker: Worker, sqs, sfn, tracer, queue_url: str):
        self.worker = worker
        self.sqs = sqs
        self.sfn = sfn
        self.tracer = tracer
        self.queue_url = queue_url

    async def execute(self, body: dict) -> dict:
        request = body["request"]
        if body["type"] == "run":
            return await self.worker.run(to_ticket(request), request["run_id"])
        return await self.worker.resume(request["run_id"], body["answer"])

    def queue_spans(self, request: dict, sent_ms: int, received_ms: float, context) -> None:
        for name, start, end in (
            ("intake", request["queued_at_ms"], sent_ms),
            ("work_queue", sent_ms, received_ms),
        ):
            span = self.tracer.start_span(name, context=context, start_time=to_ns(start))
            span.end(end_time=to_ns(end))

    async def process(self, message: dict) -> None:
        received_ms = time.time() * 1000
        body = json.loads(message["Body"])
        request = body["request"]
        context = context_from(request["traceparent"])
        sent_ms = int(message["Attributes"]["SentTimestamp"])
        if body["type"] == "run":
            self.queue_spans(request, sent_ms, received_ms, context)
        with self.tracer.start_as_current_span(f"worker {body['type']}", context=context) as span:
            span.set_attribute("request.id", request["id"])
            out = await self.execute(body)
            span.set_attribute("run.status", out["status"])
        await asyncio.to_thread(
            self.sfn.send_task_success,
            taskToken=body["task_token"],
            output=json.dumps(summary(out)),
        )
        await asyncio.to_thread(
            self.sqs.delete_message,
            QueueUrl=self.queue_url,
            ReceiptHandle=message["ReceiptHandle"],
        )

    async def poll_once(self) -> int:
        response = await asyncio.to_thread(
            self.sqs.receive_message,
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=WAIT_S,
            MessageSystemAttributeNames=["SentTimestamp"],
        )
        messages = response.get("Messages", [])
        for message in messages:
            try:
                await self.process(message)
            except Exception as e:  # it stays on the queue, so SQS retries it, then the DLQ
                print(f"message failed: {type(e).__name__}: {e}")
        return len(messages)


def approve(table, sfn, request_id: str, answer: dict) -> None:
    item = table.get_item(Key={"request_id": request_id}).get("Item")
    if not item or "approval_token" not in item:
        raise SystemExit(f"{request_id} is not waiting for approval")
    sfn.send_task_success(taskToken=item["approval_token"], output=json.dumps(answer))
    print(f"{request_id}: sent {answer['decision']} by {answer['by']}")


async def poll_until_stopped(queue: QueueWorker, flush, stop: list, once: bool = False) -> int:
    """Keep polling. A signal handler appends to `stop`; the current message finishes first."""
    handled = 0
    while not stop:
        HEARTBEAT.touch()
        handled += await queue.poll_once()
        flush()
        if once:
            break
    return handled


async def setup_checkpoints() -> None:
    async with AsyncPostgresSaver.from_conn_string(db_url()) as saver:
        await saver.setup()


async def serve(args) -> None:
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    llm = LLMClient.from_env()
    api = MockApi(mock_api_url())
    deps = Deps(llm=llm, retriever=HybridRetriever(db_url()), api=api)
    jira = JiraClient.from_env() if os.getenv("JIRA_API_TOKEN") else None
    provider = make_provider()
    tracer = provider.get_tracer("ops-agent")
    trace_http(api.client, tracer)
    history = RunHistory(db_url())
    meta = {"model": llm.model, **git_state()}

    def flush():
        provider.force_flush()
        llm.tracer.flush()

    async with AsyncPostgresSaver.from_conn_string(db_url()) as saver:
        await saver.setup()
        worker = Worker(build_graph(deps, saver), deps, jira, history, meta)
        queue = QueueWorker(
            worker,
            boto3.client("sqs"),
            boto3.client("stepfunctions"),
            tracer,
            os.environ["OPS_WORK_QUEUE_URL"],
        )
        handled = await poll_until_stopped(queue, flush, stop, args.once)
        print(f"handled {handled} messages")
    if jira:
        await jira.aclose()
    await api.aclose()


if __name__ == "__main__":
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--approve", metavar="REQUEST_ID")
    parser.add_argument("--decision", choices=["approve", "reject", "edit"])
    parser.add_argument("--by")
    parser.add_argument("--args", help="JSON arguments for --decision edit")
    args = parser.parse_args()
    if args.approve:
        if not (args.decision and args.by):
            parser.error("--approve needs --decision and --by")
        answer = {"decision": args.decision, "by": args.by}
        if args.args:
            answer["args"] = json.loads(args.args)
        table = boto3.resource("dynamodb").Table(os.environ["OPS_RUNS_TABLE"])
        approve(table, boto3.client("stepfunctions"), args.approve, answer)
    else:
        # psycopg's async mode does not work with Windows' default event loop.
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        main = setup_checkpoints() if args.setup else serve(args)
        asyncio.run(main, loop_factory=loop_factory)
