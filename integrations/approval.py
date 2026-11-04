"""Send a human decision to a run waiting at WaitForApproval in Step Functions.

The dashboard and the --approve command use this. Neither executes anything: the worker picks up
the resume message and does the work.
"""

import json

from botocore.exceptions import ClientError

TOKEN_GONE = {"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"}


class NotWaiting(Exception):
    pass


def approve(table, sfn, request_id: str, answer: dict) -> None:
    item = table.get_item(Key={"request_id": request_id}).get("Item")
    if not item or "approval_token" not in item:
        raise NotWaiting(f"{request_id} is not waiting for approval")
    try:
        sfn.send_task_success(taskToken=item["approval_token"], output=json.dumps(answer))
    except ClientError as e:  # a decision for this run was already sent
        if e.response["Error"]["Code"] in TOKEN_GONE:
            raise NotWaiting(f"{request_id} is not waiting for approval") from e
        raise
