"""Send a request to the API Gateway endpoint. The route needs a signed (SigV4) call.

Usage: python -m integrations.submit REQ-T1 ben.okafor@example.com "Where is order ORD-1004?"
"""

import argparse
import json
import os

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from dotenv import load_dotenv


def signed_post(url: str, body: str, session: boto3.Session) -> httpx.Response:
    request = AWSRequest(
        method="POST", url=url, data=body, headers={"Content-Type": "application/json"}
    )
    credentials = session.get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "execute-api", session.region_name).add_auth(request)
    return httpx.post(url, content=body, headers=dict(request.headers), timeout=15)


if __name__ == "__main__":
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("id")
    parser.add_argument("customer_email")
    parser.add_argument("request_text")
    args = parser.parse_args()
    url = f"{os.environ['OPS_API_URL'].rstrip('/')}/requests"
    response = signed_post(url, json.dumps(vars(args)), boto3.Session())
    print(response.status_code, response.text[:200])
