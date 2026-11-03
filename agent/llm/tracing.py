"""Tracing. An observation is started before the work and ended after it, so latency is real.

LangfuseTracer puts everything with the same run_id in one Langfuse trace; NullTracer drops it.
"""

from typing import Any, Protocol


class Observation(Protocol):
    def end(
        self,
        output: Any = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
    ) -> None: ...


class Tracer(Protocol):
    def start(
        self, run_id: str, name: str, input: Any, model: str | None = None
    ) -> Observation: ...

    def flush(self) -> None: ...


class NullObservation:
    def end(self, *args, **kwargs) -> None:
        pass


class NullTracer:
    def start(self, run_id, name, input, model=None) -> Observation:
        return NullObservation()

    def flush(self) -> None:
        pass


class LangfuseObservation:
    def __init__(self, observation):
        self.observation = observation

    def end(self, output=None, input_tokens=None, output_tokens=None, cost_usd=None, error=None):
        update: dict[str, Any] = {"output": output}
        if input_tokens is not None:
            update["usage_details"] = {"input": input_tokens, "output": output_tokens or 0}
        if cost_usd is not None:
            update["cost_details"] = {"total": cost_usd}
        if error:
            update["level"] = "WARNING"
            update["status_message"] = error
        self.observation.update(**update)
        self.observation.end()


class LangfuseTracer:
    """A call with a model is a generation; anything else (a graph node) is a span."""

    def __init__(self):
        from langfuse import Langfuse

        # Reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL from env.
        self.langfuse = Langfuse()

    def trace_id(self, run_id: str) -> str:
        return self.langfuse.create_trace_id(seed=run_id)

    def trace_url(self, run_id: str) -> str | None:
        return self.langfuse.get_trace_url(trace_id=self.trace_id(run_id))

    def start(self, run_id, name, input, model=None):
        observation = self.langfuse.start_observation(
            trace_context={"trace_id": self.trace_id(run_id)},
            name=name,
            as_type="generation" if model else "span",
            model=model,
            input=input,
            metadata={"run_id": run_id},
        )
        return LangfuseObservation(observation)

    def flush(self):
        self.langfuse.flush()
