"""The one entry point for LLM calls. Every call is budget-checked, then logged with its cost."""

import os
import uuid
from dataclasses import dataclass

from agent.llm.budget import Budget, BudgetExceeded, estimate_tokens
from agent.llm.pricing import cost_usd
from agent.llm.providers import AnthropicProvider, Provider
from agent.llm.tracing import LangfuseTracer, NullTracer, Tracer


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: str


class LLMRun:
    """One agent run: its own budget and its own trace."""

    def __init__(self, provider: Provider, tracer: Tracer, model: str, budget: Budget, run_id: str):
        self.provider = provider
        self.tracer = tracer
        self.model = model
        self.budget = budget
        self.run_id = run_id

    def complete(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 1024,
        name: str = "llm_call",
        model: str | None = None,
    ) -> LLMResponse:
        model = model or self.model
        prompt_text = system + "".join(str(m.get("content", "")) for m in messages)
        trace_input = {"system": system, "messages": messages}
        try:
            self.budget.check(model, estimate_tokens(prompt_text), max_tokens)
        except BudgetExceeded as e:
            self.tracer.log_call(
                self.run_id, name, model, trace_input, None, 0, 0, 0.0, error=f"refused: {e}"
            )
            raise

        try:
            result = self.provider.call(model, system, messages, max_tokens)
        except Exception as e:
            self.tracer.log_call(
                self.run_id, name, model, trace_input, None, 0, 0, 0.0, error=f"failed: {e}"
            )
            raise
        cost = cost_usd(model, result.input_tokens, result.output_tokens)
        self.budget.record(cost)
        self.tracer.log_call(
            self.run_id,
            name,
            model,
            trace_input,
            result.text,
            result.input_tokens,
            result.output_tokens,
            cost,
        )
        return LLMResponse(
            text=result.text,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost,
            stop_reason=result.stop_reason,
        )


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        tracer: Tracer,
        model: str,
        max_tokens_per_request: int,
        max_spend_per_run_usd: float,
        max_iterations: int,
    ):
        self.provider = provider
        self.tracer = tracer
        self.model = model
        self.max_tokens_per_request = max_tokens_per_request
        self.max_spend_per_run_usd = max_spend_per_run_usd
        self.max_iterations = max_iterations

    def start_run(self, run_id: str | None = None) -> LLMRun:
        budget = Budget(
            max_tokens_per_request=self.max_tokens_per_request,
            max_spend_usd=self.max_spend_per_run_usd,
            max_iterations=self.max_iterations,
        )
        return LLMRun(self.provider, self.tracer, self.model, budget, run_id or uuid.uuid4().hex)

    @classmethod
    def from_env(cls, provider: Provider | None = None) -> "LLMClient":
        tracing_on = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
        return cls(
            provider=provider or AnthropicProvider(),
            tracer=LangfuseTracer() if tracing_on else NullTracer(),
            model=os.getenv("LLM_MODEL", "claude-haiku-5-5"),
            max_tokens_per_request=int(os.getenv("LLM_MAX_TOKENS_PER_REQUEST", "8000")),
            max_spend_per_run_usd=float(os.getenv("LLM_MAX_SPEND_PER_RUN_USD", "0.05")),
            max_iterations=int(os.getenv("LLM_MAX_ITERATIONS", "10")),
        )
