"""OpenTelemetry for the queue worker: one trace per request, exported over OTLP to Langfuse.

The intake Lambda creates a W3C traceparent whose trace id equals Langfuse's id for the run, and
it travels with the request through Step Functions and SQS. The worker adds the intake, queue,
worker and mock API spans to that trace, so they sit next to the agent's own spans in Langfuse.
Spans for time spent before the worker use timestamps carried by the messages.
"""

import base64
import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def make_provider() -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "ops-agent-worker"}))
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if public and secret:
        base = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{base}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def context_from(traceparent: str):
    return TraceContextTextMapPropagator().extract({"traceparent": traceparent})


def to_ns(ms: int | float) -> int:
    return int(ms * 1_000_000)


def trace_http(client, tracer) -> None:
    """Add a span for every call made with this httpx client (the agent's mock API calls)."""

    async def on_request(request):
        name = f"mock_api {request.method} {request.url.path}"
        request.extensions["otel_span"] = tracer.start_span(name)

    async def on_response(response):
        span = response.request.extensions.pop("otel_span", None)
        if span:
            span.set_attribute("http.status_code", response.status_code)
            span.end()

    client.event_hooks["request"].append(on_request)
    client.event_hooks["response"].append(on_response)
