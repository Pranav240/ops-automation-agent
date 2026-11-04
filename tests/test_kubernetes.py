"""Phase 6: the Helm chart and the scale test's pure parts. No cluster needed."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
CHART = ROOT / "deploy" / "helm" / "ops-agent"
SECRETS = {
    f"secrets.{name}": "x"
    for name in (
        "POSTGRES_PASSWORD",
        "SQL_READER_PASSWORD",
        "WORKER_DB_PASSWORD",
        "DASHBOARD_DB_PASSWORD",
        "MOCK_API_READ_TOKEN",
        "MOCK_API_WRITE_TOKEN",
        "MOCK_API_ADMIN_TOKEN",
        "DASHBOARD_VIEWER_TOKEN",
        "DASHBOARD_APPROVER_TOKEN",
    )
}
QUEUE = {"config.OPS_WORK_QUEUE_URL": "https://q"}

spec = importlib.util.spec_from_file_location("scale_test", ROOT / "scripts" / "scale_test.py")
scale_test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scale_test)

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def render(values: dict, *extra: str) -> list[dict]:
    pairs = ",".join(f"{key}={value}" for key, value in values.items())
    command = ["helm", "template", "ops-agent", str(CHART), "--set", pairs, *extra]
    out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def by_kind(docs: list[dict], kind: str) -> dict:
    return {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == kind}


@needs_helm
def test_every_container_has_limits_and_probes():
    deployments = by_kind(render({**SECRETS, **QUEUE}), "Deployment")
    assert set(deployments) == {
        "ops-agent-worker",
        "ops-agent-mock-api",
        "ops-agent-postgres",
        "ops-agent-dashboard",
    }
    for deployment in deployments.values():
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            assert {"cpu", "memory"} <= set(container["resources"]["limits"])
            assert {"cpu", "memory"} <= set(container["resources"]["requests"])
            assert "readinessProbe" in container and "livenessProbe" in container


@needs_helm
def test_keda_owns_the_worker_replica_count():
    docs = render({**SECRETS, **QUEUE})
    assert "replicas" not in by_kind(docs, "Deployment")["ops-agent-worker"]["spec"]
    trigger = by_kind(docs, "ScaledObject")["ops-agent-worker"]["spec"]["triggers"][0]
    assert trigger["type"] == "aws-sqs-queue" and trigger["metadata"]["queueURL"] == "https://q"
    assert trigger["metadata"]["scaleOnInFlight"] == "true"


@needs_helm
def test_scaling_down_leaves_time_to_finish_a_run():
    worker = by_kind(render({**SECRETS, **QUEUE}), "Deployment")["ops-agent-worker"]
    assert worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] >= 200


@needs_helm
def test_eks_uses_the_role_and_no_key():
    extra = {"keda.auth": "irsa", "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn": "r"}
    docs = render({**SECRETS, **QUEUE, **extra})
    auth = by_kind(docs, "TriggerAuthentication")["ops-agent-sqs"]["spec"]
    assert auth == {"podIdentity": {"provider": "aws", "identityOwner": "keda"}}
    annotations = by_kind(docs, "ServiceAccount")["ops-agent-worker"]["metadata"]["annotations"]
    assert annotations == {"eks.amazonaws.com/role-arn": "r"}


@needs_helm
def test_ci_values_run_without_aws():
    docs = render({}, "-f", str(ROOT / "deploy" / "helm" / "ci-values.yaml"))
    assert "ops-agent-worker" not in by_kind(docs, "Deployment")
    assert by_kind(docs, "ScaledObject") == {}


@needs_helm
def test_chart_refuses_to_render_without_passwords():
    with pytest.raises(subprocess.CalledProcessError):
        render(QUEUE)


def test_default_values_hold_no_secret():
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert set(values["secrets"].values()) == {""}


def test_burst_requests_are_unique_and_use_each_orders_owner():
    requests = scale_test.build_requests(45, "SCALE-1")
    assert len({r["id"] for r in requests}) == 45
    seed = json.loads(scale_test.SEED.read_text(encoding="utf-8"))["orders"]
    owners = {order["order_id"]: order["customer_email"] for order in seed}
    for request in requests:
        order_id = request["request_text"].split()[-1].rstrip("?")
        assert owners[order_id] == request["customer_email"]


def sample(t, replicas, ready=0, visible=0, in_flight=0):
    return {
        "t_s": t,
        "replicas": replicas,
        "ready": ready,
        "visible": visible,
        "in_flight": in_flight,
    }


def test_summary_of_a_full_scale_cycle():
    samples = [
        sample(0, 0, visible=3),
        sample(5, 2, ready=1, visible=10),
        sample(10, 5, ready=4, visible=8, in_flight=5),
        sample(15, 5, ready=5, in_flight=2),
        sample(20, 0),
    ]
    assert scale_test.summarize(samples) == {
        "peak_replicas": 5,
        "peak_ready": 5,
        "peak_queue": 13,
        "first_scale_up_s": 5,
        "reached_peak_s": 10,
        "last_worker_seen_s": 15,
        "back_to_zero": True,
    }


def test_summary_notices_workers_that_never_came_down():
    summary = scale_test.summarize([sample(0, 0), sample(5, 3, ready=3)])
    assert summary["back_to_zero"] is False
