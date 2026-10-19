"""Phase 0 check: one real traced call, then one call that must be refused by the cap.

Usage: .venv/Scripts/python.exe scripts/smoke_llm.py
"""

from dotenv import load_dotenv

from agent.llm import BudgetExceeded, LLMClient
from agent.llm.tracing import LangfuseTracer

load_dotenv()

client = LLMClient.from_env()
run = client.start_run()
print(f"run_id={run.run_id} model={run.model} tracer={type(client.tracer).__name__}")

response = run.complete(
    [{"role": "user", "content": "Reply with exactly: pong"}], max_tokens=200, name="smoke_ok"
)
print(
    f"reply={response.text!r} in={response.input_tokens} out={response.output_tokens} "
    f"cost=${response.cost_usd:.6f}"
)

try:
    run.complete(
        [{"role": "user", "content": "This should never be sent"}],
        max_tokens=client.max_tokens_per_request + 1,
        name="smoke_over_cap",
    )
    print("ERROR: over-cap call was not refused")
except BudgetExceeded as e:
    print(f"refused as expected: {e}")

client.tracer.flush()
if isinstance(client.tracer, LangfuseTracer):
    print(f"trace: {client.tracer.trace_url(run.run_id)}")
