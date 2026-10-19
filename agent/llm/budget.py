"""Per-run limits: token cap per request, spend cap per run, max calls per run."""

from dataclasses import dataclass

from agent.llm.pricing import cost_usd


class BudgetExceeded(Exception):
    pass


def estimate_tokens(text: str) -> int:
    # Conservative: ~3 chars per token (real ratio is closer to 4 for English).
    return len(text) // 3 + 1


@dataclass
class Budget:
    max_tokens_per_request: int
    max_spend_usd: float
    max_iterations: int
    spent_usd: float = 0.0
    calls: int = 0

    def check(self, model: str, input_tokens: int, max_output_tokens: int) -> None:
        """Raise BudgetExceeded if a call with this worst case is not allowed."""
        requested = input_tokens + max_output_tokens
        if requested > self.max_tokens_per_request:
            raise BudgetExceeded(
                f"request needs up to {requested} tokens, cap is {self.max_tokens_per_request}"
            )
        if self.calls >= self.max_iterations:
            raise BudgetExceeded(f"run reached max iterations ({self.max_iterations})")
        worst_case = cost_usd(model, input_tokens, max_output_tokens)
        if self.spent_usd + worst_case > self.max_spend_usd:
            raise BudgetExceeded(
                f"run spend ${self.spent_usd:.4f} + worst case ${worst_case:.4f} "
                f"exceeds cap ${self.max_spend_usd:.4f}"
            )

    def record(self, cost: float) -> None:
        self.calls += 1
        self.spent_usd += cost
