import pytest

from agent.llm import Budget, BudgetExceeded, FakeProvider, LLMClient
from agent.llm.pricing import cost_usd
from agent.llm.tracing import NullTracer


class RecordingTracer(NullTracer):
    def __init__(self):
        self.calls = []

    def log_call(
        self, run_id, name, model, input, output, input_tokens, output_tokens, cost_usd, error=None
    ):
        self.calls.append(
            {
                "run_id": run_id,
                "model": model,
                "output": output,
                "cost_usd": cost_usd,
                "error": error,
            }
        )


def make_client(replies, tracer=None, **limits):
    settings = {"max_tokens_per_request": 2000, "max_spend_per_run_usd": 0.01, "max_iterations": 3}
    settings.update(limits)
    return LLMClient(
        provider=FakeProvider(replies=list(replies)),
        tracer=tracer or NullTracer(),
        model="claude-haiku-5-5",
        **settings,
    )


def ask(run, text="hi", max_tokens=100):
    return run.complete([{"role": "user", "content": text}], max_tokens=max_tokens)


def test_cost_uses_model_price():
    # Haiku 5.5: $0.10 in / $0.50 out per 1M tokens
    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)


def test_unknown_model_has_no_price():
    with pytest.raises(KeyError):
        cost_usd("not-a-model", 1, 1)


def test_call_returns_text_and_logs_cost():
    tracer = RecordingTracer()
    run = make_client(["hello"], tracer=tracer).start_run("run-1")
    response = ask(run)
    assert response.text == "hello"
    assert response.cost_usd == pytest.approx(cost_usd("claude-haiku-5-5", 10, 5))
    assert tracer.calls == [
        {
            "run_id": "run-1",
            "model": "claude-haiku-5-5",
            "output": "hello",
            "cost_usd": response.cost_usd,
            "error": None,
        }
    ]
    assert run.budget.calls == 1


def test_request_over_token_cap_is_refused_before_calling_provider():
    tracer = RecordingTracer()
    client = make_client(["never"], tracer=tracer, max_tokens_per_request=500)
    run = client.start_run()
    with pytest.raises(BudgetExceeded, match="cap is 500"):
        ask(run, max_tokens=600)
    assert client.provider.calls == []
    assert tracer.calls[0]["error"].startswith("refused")


def test_run_over_spend_cap_is_refused():
    # Worst case for 100 output tokens on Haiku is ~$0.00005, so a tiny cap refuses it.
    client = make_client(["never"], max_spend_per_run_usd=0.00001)
    with pytest.raises(BudgetExceeded, match="exceeds cap"):
        ask(client.start_run())
    assert client.provider.calls == []


def test_run_stops_at_max_iterations():
    client = make_client(["a", "b", "c"], max_iterations=2)
    run = client.start_run()
    ask(run)
    ask(run)
    with pytest.raises(BudgetExceeded, match="max iterations"):
        ask(run)


def test_each_run_gets_a_fresh_budget():
    client = make_client(["a", "b"], max_iterations=1)
    ask(client.start_run())
    ask(client.start_run())


def test_spend_accumulates_across_calls():
    budget = Budget(max_tokens_per_request=1000, max_spend_usd=1.0, max_iterations=5)
    budget.record(0.2)
    budget.record(0.3)
    assert budget.spent_usd == pytest.approx(0.5)
    assert budget.calls == 2


def test_provider_error_is_logged_and_raised():
    tracer = RecordingTracer()
    run = make_client([], tracer=tracer).start_run()
    with pytest.raises(RuntimeError):
        ask(run)
    assert tracer.calls[0]["error"].startswith("failed")
    assert run.budget.calls == 0


def test_from_env_without_langfuse_keys_uses_null_tracer(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("LLM_MAX_SPEND_PER_RUN_USD", "0.02")
    client = LLMClient.from_env(provider=FakeProvider(replies=[]))
    assert isinstance(client.tracer, NullTracer)
    assert client.max_spend_per_run_usd == 0.02
