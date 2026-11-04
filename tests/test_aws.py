"""Phase 5 code against fake AWS clients: the two Lambdas, the queue worker and signed requests.
The agent runs as in test_jira. No network, no AWS account, no real LLM call."""

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.config import git_state
from agent.llm import BedrockProvider, LLMClient
from agent.llm.pricing import cost_usd
from integrations import submit
from integrations.approval import NotWaiting, approve
from integrations.otel import context_from, to_ns, trace_http
from integrations.queue_worker import (
    HEARTBEAT,
    QueueWorker,
    poll_until_stopped,
    summary,
    to_ticket,
)
from tests.test_agent import ALICE, FARID, final, lookup, plan, refund, review, written_actions
from tests.test_jira import setup as worker_setup

LAMBDAS = Path(__file__).parent.parent / "infra" / "aws" / "lambdas"
REQUEST = {"id": "REQ-1", "customer_email": ALICE, "request_text": "My mug arrived broken."}


def load_lambda(name: str, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    spec = importlib.util.spec_from_file_location(f"{name}_handler", LAMBDAS / name / "handler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression):
        if Item["request_id"] in self.items:
            error = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise ClientError(error, "PutItem")
        self.items[Item["request_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["request_id"])
        return {"Item": item} if item else {}

    def update_item(self, Key, ExpressionAttributeValues, **_):
        self.items[Key["request_id"]]["status"] = ExpressionAttributeValues[":s"]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body


class FakeSfn:
    class exceptions:
        class ExecutionAlreadyExists(Exception):
            pass

    def __init__(self, fail_first=False):
        self.started = []
        self.fail_first = fail_first
        self.successes = []

    def start_execution(self, stateMachineArn, name, input):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("step functions unavailable")
        if name in [run["name"] for run in self.started]:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started.append({"name": name, "input": json.loads(input)})

    def send_task_success(self, taskToken, output):
        self.successes.append((taskToken, json.loads(output)))


class FakeSqs:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.sent = []
        self.deleted = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(json.loads(MessageBody))

    def receive_message(self, **_):
        return {"Messages": [self.messages.pop(0)]} if self.messages else {}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


@pytest.fixture
def intake(monkeypatch):
    module = load_lambda("intake", monkeypatch)
    module.s3, module.sfn, module.table = FakeS3(), FakeSfn(), FakeTable()
    monkeypatch.setenv("ATTACHMENTS_BUCKET", "bucket")
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    return module


def intake_record(body: dict) -> dict:
    return {"body": json.dumps(body), "attributes": {"SentTimestamp": "1000"}}


def test_intake_starts_one_run_and_keeps_a_copy(intake):
    intake.handler({"Records": [intake_record(REQUEST)]}, None)
    run = intake.sfn.started[0]
    payload = run["input"]["request"]
    assert run["name"] == intake.run_id_for("REQ-1") == payload["run_id"]
    assert payload["queued_at_ms"] == 1000 and payload["request_text"] == REQUEST["request_text"]
    assert intake.table.items["REQ-1"]["status"] == "started"
    assert json.loads(intake.s3.objects[f"requests/{run['name']}.json"]) == REQUEST


def test_intake_drops_a_duplicate_request(intake):
    for _ in range(2):
        intake.handler({"Records": [intake_record(REQUEST)]}, None)
    assert len(intake.sfn.started) == 1


def test_intake_retry_finishes_a_run_that_failed_to_start(intake):
    intake.sfn.fail_first = True
    with pytest.raises(RuntimeError):
        intake.handler({"Records": [intake_record(REQUEST)]}, None)
    intake.handler({"Records": [intake_record(REQUEST)]}, None)
    assert len(intake.sfn.started) == 1 and intake.table.items["REQ-1"]["status"] == "started"


@pytest.mark.parametrize(
    "bad",
    [
        {"customer_email": ALICE, "request_text": "x"},
        {"id": "R", "customer_email": ALICE, "request_text": "   "},
        {"id": "R", "customer_email": ALICE, "request_text": "x" * 5001},
        {"id": 7, "customer_email": ALICE, "request_text": "x"},
    ],
)
def test_intake_raises_on_a_bad_message_so_it_reaches_the_dlq(intake, bad):
    with pytest.raises(ValueError):
        intake.handler({"Records": [intake_record(bad)]}, None)
    assert intake.sfn.started == [] and intake.table.items == {}


def test_intake_raises_on_a_message_that_is_not_json(intake):
    with pytest.raises(ValueError):
        intake.handler({"Records": [{"body": "not json", "attributes": {}}]}, None)


def test_trace_id_equals_langfuse_id_for_the_run(intake):
    run_id = intake.run_id_for("REQ-1")
    trace_id = intake.traceparent(run_id).split("-")[1]
    assert trace_id == Langfuse.create_trace_id(seed=run_id)
    assert trace_id == hashlib.sha256(run_id.encode()).digest()[:16].hex()


def test_jira_poll_sends_the_ticket_before_labelling_it(monkeypatch):
    module = load_lambda("jira_poll", monkeypatch)
    calls = []

    class Ssm:
        def get_parameter(self, Name, WithDecryption):
            assert WithDecryption
            return {"Parameter": {"Value": "secret"}}

    class Sqs(FakeSqs):
        def send_message(self, QueueUrl, MessageBody):
            calls.append("send")
            super().send_message(QueueUrl, MessageBody)

    def jira(method, path, token, body=None):
        assert token == "secret"
        if path.startswith("/rest/api/3/search"):
            assert "IT" in path
            return {"issues": [{"key": "IT-3"}]}
        if method == "GET":
            return {
                "reporter": {"emailAddress": ALICE},
                "requestFieldValues": [
                    {"fieldId": "summary", "value": "Broken mug"},
                    {"fieldId": "description", "value": "It arrived broken."},
                ],
            }
        calls.append("label")
        assert body == {"update": {"labels": [{"add": module.LABEL}]}}
        return {}

    module.ssm, module.sqs, module.jira = Ssm(), Sqs(), jira
    for name, value in (("JIRA_TOKEN_PARAM", "/p"), ("JIRA_PROJECT_KEY", "IT")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("INTAKE_QUEUE_URL", "https://q")
    module.handler({}, None)
    assert calls == ["send", "label"]
    assert module.sqs.sent == [
        {
            "id": "IT-3",
            "ticket_key": "IT-3",
            "customer_email": ALICE,
            "request_text": "Broken mug\n\nIt arrived broken.",
        }
    ]


def queue_setup(replies, messages):
    fake, worker = worker_setup(replies)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sqs, sfn = FakeSqs(messages), FakeSfn()
    queue = QueueWorker(worker, sqs, sfn, provider.get_tracer("t"), "https://q")
    return fake, worker, queue, exporter


def message(body: dict, handle: str = "h1") -> dict:
    return {
        "Body": json.dumps(body),
        "Attributes": {"SentTimestamp": "2000"},
        "ReceiptHandle": handle,
    }


def run_message(email=ALICE, text="My mug from ORD-1001 arrived broken.", **extra) -> dict:
    request = {
        "id": "REQ-1",
        "run_id": "a" * 32,
        "customer_email": email,
        "request_text": text,
        "traceparent": f"00-{'b' * 32}-{'c' * 16}-01",
        "queued_at_ms": 1000,
        **extra,
    }
    return message({"type": "run", "request": request, "task_token": "token-1"})


def small_refund():
    return [plan(), lookup("ORD-1001"), refund("ORD-1001", 65.0), final(reply="Done."), review()]


def large_refund():
    return [plan(), lookup("ORD-1011"), refund("ORD-1011", 649.0), final(), review()]


def test_api_request_finishes_and_nothing_goes_to_jira():
    fake, worker, queue, _ = queue_setup(small_refund(), [run_message()])
    assert asyncio.run(queue.poll_once()) == 1
    assert queue.sfn.successes == [
        ("token-1", {"status": "done", "run_id": "a" * 32, "action": "refund"})
    ]
    assert queue.sqs.deleted == ["h1"]
    assert fake.issues == {}
    row = worker.history.rows[0]
    assert row["source"] == "api" and row["ticket_key"] is None and row["request_id"] == "REQ-1"


def test_empty_poll_does_nothing():
    _, _, queue, _ = queue_setup([], [])
    assert asyncio.run(queue.poll_once()) == 0


def test_jira_message_posts_the_reply_to_the_ticket():
    fake, _, queue, _ = queue_setup(small_refund(), [run_message(ticket_key="SUP-1")])
    fake.add("SUP-1", ALICE, "Broken mug", "My mug from ORD-1001 arrived broken.")
    asyncio.run(queue.poll_once())
    assert fake.issues["SUP-1"]["comments"] == [("Done.", True)]


def test_a_message_delivered_twice_refunds_once():
    messages = [run_message(), run_message()]
    _, _, queue, _ = queue_setup(small_refund(), messages)
    asyncio.run(queue.poll_once())
    asyncio.run(queue.poll_once())
    assert len(written_actions()) == 1 and len(queue.sfn.successes) == 2


def test_failed_run_leaves_the_message_on_the_queue():
    _, _, queue, _ = queue_setup([], [run_message()])  # no replies: the fake model raises
    assert asyncio.run(queue.poll_once()) == 1  # the worker keeps running
    assert queue.sqs.deleted == [] and queue.sfn.successes == []


def test_risky_run_waits_then_resumes_after_approval():
    start = run_message(email=FARID, text="Espresso machine ORD-1011 arrived broken.")
    fake, worker, queue, _ = queue_setup(large_refund(), [start])
    asyncio.run(queue.poll_once())
    assert queue.sfn.successes[0][1]["status"] == "pending_approval"
    assert written_actions() == []

    body = json.loads(start["Body"])
    resumed = {
        "type": "resume",
        "request": body["request"],
        "answer": {"decision": "approve", "by": "owner"},
        "task_token": "token-2",
    }
    queue.sqs.messages = [message(resumed, "h2"), message(resumed, "h3")]  # delivered twice
    asyncio.run(queue.poll_once())
    asyncio.run(queue.poll_once())
    assert queue.sfn.successes[1] == (
        "token-2",
        {"status": "done", "run_id": "a" * 32, "action": "refund"},
    )
    assert len(written_actions()) == 1 and written_actions()[0]["approved_by"] == "owner"
    assert queue.sqs.deleted == ["h1", "h2", "h3"]


def test_stop_signal_ends_the_loop_after_the_current_message(tmp_path):
    _, _, queue, _ = queue_setup(small_refund(), [run_message(), run_message()])
    stop, flushed = [], []

    def flush():  # stands in for a SIGTERM that arrives during the first message
        flushed.append(True)
        stop.append(True)

    HEARTBEAT.unlink(missing_ok=True)
    assert asyncio.run(poll_until_stopped(queue, flush, stop)) == 1
    assert flushed == [True] and queue.sqs.deleted == ["h1"] and len(queue.sqs.messages) == 1
    assert HEARTBEAT.exists()


def test_stopped_worker_takes_no_message():
    _, _, queue, _ = queue_setup([], [run_message()])
    assert asyncio.run(poll_until_stopped(queue, lambda: None, [True])) == 0
    assert len(queue.sqs.messages) == 1


def test_git_commit_setting_is_used_in_images(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    assert git_state() == {"commit": "abc1234", "dirty": False}


def test_spans_share_the_trace_and_cover_the_queue():
    _, _, queue, exporter = queue_setup(small_refund(), [run_message()])
    asyncio.run(queue.poll_once())
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"intake", "work_queue", "worker run"}
    assert {f"{span.context.trace_id:032x}" for span in spans.values()} == {"b" * 32}
    assert spans["intake"].start_time == to_ns(1000) and spans["intake"].end_time == to_ns(2000)
    assert spans["work_queue"].start_time == to_ns(2000)
    assert spans["worker run"].attributes["run.status"] == "done"


def test_mock_api_calls_become_spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.AsyncClient(base_url="https://api", transport=transport)
    trace_http(client, provider.get_tracer("t"))
    asyncio.run(client.get("/orders/1"))
    span = exporter.get_finished_spans()[0]
    assert span.name == "mock_api GET /orders/1"
    assert span.attributes["http.status_code"] == 200


def test_context_keeps_the_trace_id():
    from opentelemetry import trace

    context = context_from(f"00-{'b' * 32}-{'c' * 16}-01")
    assert f"{trace.get_current_span(context).get_span_context().trace_id:032x}" == "b" * 32


def test_to_ticket_keeps_api_and_jira_requests_apart():
    api = to_ticket({"id": "R", "customer_email": ALICE, "request_text": "x"})
    assert api.source == "api" and api.key == "R"
    jira = to_ticket(
        {"id": "IT-1", "ticket_key": "IT-1", "customer_email": ALICE, "request_text": "x"}
    )
    assert jira.source == "jira" and jira.key == "IT-1"


def test_summary_keeps_only_what_step_functions_needs():
    out = {"status": "done", "run_id": "r", "action": "refund", "reply": "secret customer details"}
    assert summary(out) == {"status": "done", "run_id": "r", "action": "refund"}


def test_approve_sends_the_stored_token():
    table, sfn = FakeTable(), FakeSfn()
    table.items["REQ-1"] = {"request_id": "REQ-1", "approval_token": "t"}
    approve(table, sfn, "REQ-1", {"decision": "approve", "by": "owner"})
    assert sfn.successes == [("t", {"decision": "approve", "by": "owner"})]


def test_approve_refuses_a_request_that_is_not_waiting():
    table = FakeTable()
    table.items["REQ-1"] = {"request_id": "REQ-1"}
    with pytest.raises(NotWaiting):
        approve(table, FakeSfn(), "REQ-1", {"decision": "approve", "by": "owner"})


def test_submit_signs_for_api_gateway(monkeypatch):
    sent = {}

    def post(url, content, headers, timeout):
        sent.update(url=url, content=content, headers=headers)
        return httpx.Response(200)

    monkeypatch.setattr(submit.httpx, "post", post)
    session = boto3.Session(
        aws_access_key_id="AKIAFAKE", aws_secret_access_key="fake", region_name="ap-south-1"
    )
    submit.signed_post("https://api.example.com/requests", "{}", session)
    authorization = sent["headers"]["Authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKE/")
    assert "/ap-south-1/execute-api/aws4_request" in authorization


def test_llm_provider_setting_selects_bedrock(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("LLM_MODEL", "global.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    client = LLMClient.from_env()
    assert isinstance(client.provider, BedrockProvider) and client.provider.name == "bedrock"


def test_bedrock_model_has_a_price():
    model = "global.anthropic.claude-haiku-5-5"
    assert cost_usd(model, 1_000_000, 1_000_000) == pytest.approx(0.60)
