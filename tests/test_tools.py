import asyncio

import httpx
import pytest

from agent.tools import TOOL_SCHEMAS, WRITE_TOOLS, MockApi, validate_args
from mock_api.app import app, store


def test_validate_args_accepts_good_refund():
    args = {"order_id": "ORD-1001", "amount": 65, "reason": "lost"}
    assert validate_args("refund", args) == {
        "order_id": "ORD-1001",
        "amount": 65.0,
        "reason": "lost",
    }


@pytest.mark.parametrize(
    "action,args",
    [
        ("refund", {"order_id": "1001", "amount": 5, "reason": "lost"}),
        ("refund", {"order_id": "ORD-1001", "amount": 0, "reason": "lost"}),
        ("refund", {"order_id": "ORD-1001", "amount": 5, "reason": "goodwill"}),
        ("refund", {"order_id": "ORD-1001", "amount": 5, "reason": "lost", "approved_by": "me"}),
        ("change_status", {"order_id": "ORD-1001", "status": "shipped"}),
        ("escalate", {"order_id": None, "reason": "angry", "summary": "x"}),
        ("close", {"resolution": ""}),
    ],
)
def test_validate_args_rejects_bad_input(action, args):
    with pytest.raises(ValueError):
        validate_args(action, args)


def test_every_write_tool_has_a_strict_schema():
    by_name = {t["name"]: t for t in TOOL_SCHEMAS}
    for name in WRITE_TOOLS:
        schema = by_name[name]
        assert schema["strict"] and schema["input_schema"]["additionalProperties"] is False


def test_execute_against_mock_api():
    store.reset()
    api = MockApi("http://mock", transport=httpx.ASGITransport(app=app))
    ok = asyncio.run(
        api.execute(
            "refund", {"order_id": "ORD-1001", "amount": 65.0, "reason": "lost"}, "T1", None
        )
    )
    assert ok.ok and ok.body["action"] == "refund"
    big = asyncio.run(
        api.execute(
            "refund", {"order_id": "ORD-1011", "amount": 649.0, "reason": "lost"}, "T1", None
        )
    )
    assert big.status == 403 and "approval" in big.body


def flaky_transport(fail_times, status=None):
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) <= fail_times:
            if status:
                return httpx.Response(status, json={"detail": "boom"})
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"order_id": "ORD-1001"})

    return httpx.MockTransport(handler), calls


def test_get_retries_server_errors_with_backoff():
    transport, calls = flaky_transport(2, status=503)
    api = MockApi("http://mock", transport=transport, backoff_s=0)
    assert asyncio.run(api.get_order("ORD-1001")).ok
    assert len(calls) == 3


def test_post_is_not_retried_after_a_server_error():
    transport, calls = flaky_transport(1, status=500)
    api = MockApi("http://mock", transport=transport, backoff_s=0)
    result = asyncio.run(
        api.execute("escalate", {"order_id": None, "reason": "other", "summary": "x"}, "T1", None)
    )
    assert result.status == 500 and len(calls) == 1


def test_post_is_retried_when_connection_failed():
    transport, calls = flaky_transport(1)
    api = MockApi("http://mock", transport=transport, backoff_s=0)
    result = asyncio.run(api.execute("close", {"resolution": "done"}, "T1", None))
    assert result.ok and len(calls) == 2


def test_gives_up_after_retries():
    transport, calls = flaky_transport(10)
    api = MockApi("http://mock", transport=transport, retries=2, backoff_s=0)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(api.get_order("ORD-1001"))
    assert len(calls) == 3
