"""Intake Lambda: SQS intake queue -> validate -> S3 copy -> run record -> Step Functions.

A bad message raises, so SQS retries it and then moves it to the intake DLQ. The request id is
the idempotency key: a second request with the same id does not start a second run.
"""

import hashlib
import json
import os

import boto3
from botocore.exceptions import ClientError

MAX_TEXT = 5000
s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")
table = boto3.resource("dynamodb").Table(os.environ.get("RUNS_TABLE", "unset"))


def parse(body: str) -> dict:
    request = json.loads(body)
    for field in ("id", "customer_email", "request_text"):
        if not isinstance(request.get(field), str) or not request[field].strip():
            raise ValueError(f"missing field {field}")
    if len(request["request_text"]) > MAX_TEXT:
        raise ValueError("request_text is too long")
    return request


def run_id_for(request_id: str) -> str:
    return hashlib.sha256(request_id.encode()).hexdigest()[:32]


def traceparent(run_id: str) -> str:
    """W3C trace context. The trace id matches Langfuse's id for this run (seed = run_id)."""
    trace_id = hashlib.sha256(run_id.encode()).digest()[:16].hex()
    span_id = hashlib.sha256(f"intake-{run_id}".encode()).digest()[:8].hex()
    return f"00-{trace_id}-{span_id}-01"


def claim(request: dict, run_id: str) -> bool:
    """True if this message should start the run: it is new, or an earlier try did not finish."""
    try:
        table.put_item(
            Item={"request_id": request["id"], "run_id": run_id, "status": "received"},
            ConditionExpression="attribute_not_exists(request_id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return table.get_item(Key={"request_id": request["id"]})["Item"]["status"] == "received"


def handle(record: dict) -> None:
    request = parse(record["body"])
    run_id = run_id_for(request["id"])
    if not claim(request, run_id):
        print(f"duplicate request {request['id']}")
        return
    s3.put_object(
        Bucket=os.environ["ATTACHMENTS_BUCKET"],
        Key=f"requests/{run_id}.json",
        Body=record["body"].encode(),
    )
    payload = {
        **request,
        "run_id": run_id,
        "traceparent": traceparent(run_id),
        "queued_at_ms": int(record["attributes"]["SentTimestamp"]),
    }
    try:
        sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=run_id,
            input=json.dumps({"request": payload}),
        )
    except sfn.exceptions.ExecutionAlreadyExists:
        print(f"run already started for {request['id']}")
        return
    table.update_item(
        Key={"request_id": request["id"]},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "started"},
    )
    print(f"started {request['id']} run {run_id}")


def handler(event, context):
    for record in event["Records"]:
        handle(record)
