"""Jira poll Lambda (EventBridge schedule): new service desk requests -> intake queue.

The message is sent before the ticket is labelled. If labelling fails the ticket is sent again on
the next poll, and intake drops it as a duplicate, because the ticket key is the request id.
"""

import base64
import json
import os
import urllib.parse
import urllib.request

import boto3

LABEL = "ops-agent"
sqs = boto3.client("sqs")
ssm = boto3.client("ssm")


def jira(method: str, path: str, token: str, body: dict | None = None) -> dict:
    auth = base64.b64encode(f"{os.environ['JIRA_EMAIL']}:{token}".encode()).decode()
    request = urllib.request.Request(
        os.environ["JIRA_BASE_URL"].rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        text = response.read()
    return json.loads(text) if text else {}


def to_message(key: str, service_request: dict) -> dict:
    fields = {f["fieldId"]: f.get("value") for f in service_request.get("requestFieldValues", [])}
    text = f"{fields.get('summary') or ''}\n\n{fields.get('description') or ''}".strip()
    return {
        "id": key,
        "ticket_key": key,
        "customer_email": service_request["reporter"].get("emailAddress", ""),
        "request_text": text,
    }


def handler(event, context):
    token = ssm.get_parameter(Name=os.environ["JIRA_TOKEN_PARAM"], WithDecryption=True)
    token = token["Parameter"]["Value"]
    jql = (
        f'project = "{os.environ["JIRA_PROJECT_KEY"]}" AND statusCategory != Done '
        f'AND (labels IS EMPTY OR labels NOT IN ("{LABEL}")) ORDER BY created ASC'
    )
    query = urllib.parse.urlencode({"jql": jql, "fields": "summary", "maxResults": 20})
    issues = jira("GET", f"/rest/api/3/search/jql?{query}", token).get("issues", [])
    for issue in issues:
        key = issue["key"]
        service_request = jira("GET", f"/rest/servicedeskapi/request/{key}", token)
        sqs.send_message(
            QueueUrl=os.environ["INTAKE_QUEUE_URL"],
            MessageBody=json.dumps(to_message(key, service_request)),
        )
        update = {"update": {"labels": [{"add": LABEL}]}}
        jira("PUT", f"/rest/api/3/issue/{key}", token, update)
    print(f"sent {len(issues)} tickets")
