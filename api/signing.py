"""HMAC-SHA256 signatures, used both ways: on calls to this API and on the webhooks it sends.

The signature covers "<timestamp>.<raw body>", so a body cannot be changed or moved to another
time. Calls older than 5 minutes (or more than 5 minutes in the future) are refused, so a captured
call cannot be replayed later. Signatures are hex, sent as X-Signature: sha256=<hex>.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
MAX_AGE_S = 300
PREFIX = "sha256="


def sign(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + body
    return PREFIX + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, body: bytes, now: float | None = None) -> dict[str, str]:
    timestamp = str(int(time.time() if now is None else now))
    return {TIMESTAMP_HEADER: timestamp, SIGNATURE_HEADER: sign(secret, timestamp, body)}


def check(
    secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float | None = None
) -> str | None:
    """Why the call is refused, or None when the signature is good and fresh."""
    if not timestamp or not signature:
        return "missing signature headers"
    try:
        sent_at = int(timestamp)
    except ValueError:
        return "bad timestamp"
    if abs((time.time() if now is None else now) - sent_at) > MAX_AGE_S:
        return "timestamp too old or too far ahead"
    if not hmac.compare_digest(signature.encode(), sign(secret, timestamp, body).encode()):
        return "bad signature"
    return None
