"""USD prices per 1M tokens (Anthropic first-party rates, checked 2026-10-08)."""

PRICES = {
    # model: (input, output)
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens
    "claude-sonnet-5-5": (2.00, 10.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICES:
        raise KeyError(f"No price for model {model!r}; add it to PRICES")
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
