"""The one entry point for LLM calls. Every call is budget-checked, then logged with its cost."""

import json
import os
import uuid
from dataclasses import dataclass, field

from agent.llm.budget import Budget, BudgetExceeded, estimate_tokens
from agent.llm.pricing import cost_usd
from agent.llm.providers import AnthropicProvider, BedrockProvider, Provider, ToolCall
from agent.llm.tracing import LangfuseTracer, NullTracer, Tracer

PROVIDERS = {"anthropic": AnthropicProvider, "bedrock": BedrockProvider}


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: str
    content: list[dict] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)


def prompt_text(system: str, messages: list[dict], tools: list[dict] | None) -> str:
    """Text used to estimate input tokens. Thinking blocks are skipped (long opaque signatures)."""
    parts = [system, json.dumps(tools or [])]
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in content:
            if block.get("type") not in ("thinking", "redacted_thinking"):
                parts.append(json.dumps(block, default=str))
    return "".join(parts)


class LLMRun:
    """One agent run: its own budget and its own trace."""

    def __init__(self, provider: Provider, tracer: Tracer, model: str, budget: Budget, run_id: str):
        self.provider = provider
        self.tracer = tracer
        self.model = model
        self.budget = budget
        self.run_id = run_id

    async def complete(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 1024,
        name: str = "llm_call",
        model: str | None = None,
        tools: list[dict] | None = None,
        output_schema: dict | None = None,
        effort: str | None = None,
    ) -> LLMResponse:
        model = model or self.model
        observation = self.tracer.start(
            self.run_id, name, {"system": system, "messages": messages}, model=model
        )
        try:
            self.budget.check(
                model, estimate_tokens(prompt_text(system, messages, tools)), max_tokens
            )
        except BudgetExceeded as e:
            observation.end(error=f"refused: {e}")
            raise

        try:
            result = await self.provider.call(
                model, system, messages, max_tokens, tools, output_schema, effort
            )
        except Exception as e:
            observation.end(error=f"failed: {e}")
            raise
        cost = cost_usd(model, result.input_tokens, result.output_tokens)
        self.budget.record(cost)
        observation.end(
            output=result.content or result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost,
            error=None if result.stop_reason != "max_tokens" else "stopped at max_tokens",
        )
        return LLMResponse(
            text=result.text,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost,
            stop_reason=result.stop_reason,
            content=result.content,
            tool_calls=result.tool_calls,
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
            provider=provider or PROVIDERS[os.getenv("LLM_PROVIDER", "anthropic")](),
            tracer=LangfuseTracer() if tracing_on else NullTracer(),
            model=os.getenv("LLM_MODEL", "claude-haiku-5-5"),
            max_tokens_per_request=int(os.getenv("LLM_MAX_TOKENS_PER_REQUEST", "8000")),
            max_spend_per_run_usd=float(os.getenv("LLM_MAX_SPEND_PER_RUN_USD", "0.05")),
            max_iterations=int(os.getenv("LLM_MAX_ITERATIONS", "10")),
        )
