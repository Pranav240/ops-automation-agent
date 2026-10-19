"""Cost and call logging. LangfuseTracer sends each call as a generation; NullTracer drops it."""

from typing import Any, Protocol


class Tracer(Protocol):
    def log_call(
        self,
        run_id: str,
        name: str,
        model: str,
        input: Any,
        output: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        error: str | None = None,
    ) -> None: ...

    def flush(self) -> None: ...


class NullTracer:
    def log_call(self, *args, **kwargs) -> None:
        pass

    def flush(self) -> None:
        pass


class LangfuseTracer:
    """All calls with the same run_id land in one Langfuse trace."""

    def __init__(self):
        from langfuse import Langfuse

        # Reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL from env.
        self.langfuse = Langfuse()

    def trace_id(self, run_id: str) -> str:
        return self.langfuse.create_trace_id(seed=run_id)

    def trace_url(self, run_id: str) -> str | None:
        return self.langfuse.get_trace_url(trace_id=self.trace_id(run_id))

    def log_call(
        self,
        run_id,
        name,
        model,
        input,
        output,
        input_tokens,
        output_tokens,
        cost_usd,
        error=None,
    ):
        generation = self.langfuse.start_observation(
            trace_context={"trace_id": self.trace_id(run_id)},
            name=name,
            as_type="generation",
            model=model,
            input=input,
            output=output,
            usage_details={"input": input_tokens, "output": output_tokens},
            cost_details={"total": cost_usd},
            metadata={"run_id": run_id},
            level="WARNING" if error else "DEFAULT",
            status_message=error,
        )
        generation.end()

    def flush(self):
        self.langfuse.flush()
