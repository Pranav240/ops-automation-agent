"""LLM providers. Each one turns (model, system, messages, max_tokens) into a ProviderResult."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    raw: Any = None


class Provider(Protocol):
    name: str

    def call(
        self, model: str, system: str, messages: list[dict], max_tokens: int
    ) -> ProviderResult: ...


class AnthropicProvider:
    """Claude via the direct API. The SDK retries 429/5xx/connection errors with backoff."""

    name = "anthropic"

    def __init__(self, timeout_s: float = 60.0, max_retries: int = 3):
        import anthropic

        self.client = anthropic.Anthropic(timeout=timeout_s, max_retries=max_retries)

    def call(self, model, system, messages, max_tokens):
        response = self.client.messages.create(
            model=model, system=system, messages=messages, max_tokens=max_tokens
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return ProviderResult(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            raw=response,
        )


@dataclass
class FakeProvider:
    """Scripted provider for tests. Returns the given replies in order."""

    replies: list[str]
    input_tokens: int = 10
    output_tokens: int = 5
    calls: list[dict] = field(default_factory=list)
    name: str = "fake"

    def call(self, model, system, messages, max_tokens):
        self.calls.append(
            {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        )
        if not self.replies:
            raise RuntimeError("FakeProvider has no replies left")
        return ProviderResult(
            text=self.replies.pop(0),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            stop_reason="end_turn",
        )
