"""LLM providers. Each one turns one request into a ProviderResult."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    content: list[dict] = field(default_factory=list)  # assistant blocks, to append to history
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None


class Provider(Protocol):
    name: str

    async def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        output_schema: dict | None = None,
        effort: str | None = None,
    ) -> ProviderResult: ...


class AnthropicProvider:
    """Claude via the direct API. The SDK retries 429/5xx/connection errors with backoff."""

    name = "anthropic"

    def __init__(self, timeout_s: float = 60.0, max_retries: int = 3):
        import anthropic

        self.client = anthropic.AsyncAnthropic(timeout=timeout_s, max_retries=max_retries)

    async def call(
        self, model, system, messages, max_tokens, tools=None, output_schema=None, effort=None
    ):
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        output_config: dict[str, Any] = {}
        if output_schema:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if effort:
            output_config["effort"] = effort
        if output_config:
            kwargs["output_config"] = output_config
        response = await self.client.messages.create(
            model=model, system=system, messages=messages, max_tokens=max_tokens, **kwargs
        )
        return ProviderResult(
            text="".join(b.text for b in response.content if b.type == "text"),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            content=[b.to_dict() for b in response.content],
            tool_calls=[
                ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"
            ],
            raw=response,
        )


class BedrockProvider(AnthropicProvider):
    """Claude on Amazon Bedrock, signed with AWS credentials.

    Same request shape as the direct API, so only the client differs. The region and credentials
    come from the standard AWS settings. Haiku 5.5 is reached through its global inference profile:
    Bedrock's newer Mantle endpoint did not offer it in ap-south-1 or us-east-1 (2026-11-04).
    """

    name = "bedrock"

    def __init__(self, timeout_s: float = 60.0, max_retries: int = 3):
        import anthropic

        self.client = anthropic.AsyncAnthropicBedrock(timeout=timeout_s, max_retries=max_retries)


@dataclass
class FakeProvider:
    """Scripted provider for tests. Replies are returned in order.

    A reply is a string (text answer), {"tool": name, "input": {...}} (one tool call),
    or {"refusal": True} (the model's safety system declined the call).
    """

    replies: list[str | dict]
    input_tokens: int = 10
    output_tokens: int = 5
    calls: list[dict] = field(default_factory=list)
    name: str = "fake"

    async def call(
        self, model, system, messages, max_tokens, tools=None, output_schema=None, effort=None
    ):
        self.calls.append(
            {
                "model": model,
                "system": system,
                "messages": list(messages),
                "max_tokens": max_tokens,
                "tools": tools,
                "output_schema": output_schema,
            }
        )
        if not self.replies:
            raise RuntimeError("FakeProvider has no replies left")
        reply = self.replies.pop(0)
        if isinstance(reply, str):
            return ProviderResult(
                text=reply,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                stop_reason="end_turn",
                content=[{"type": "text", "text": reply}],
            )
        if reply.get("refusal"):
            return ProviderResult(
                text="", input_tokens=self.input_tokens, output_tokens=0, stop_reason="refusal"
            )
        call = ToolCall(f"toolu_{len(self.calls)}", reply["tool"], reply["input"])
        return ProviderResult(
            text="",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            stop_reason="tool_use",
            content=[{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}],
            tool_calls=[call],
        )
