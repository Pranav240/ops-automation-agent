import { useCallback, useEffect, useState } from "react";
import { api, type RunDetail, type Scope } from "./api";
import { DecisionPanel } from "./DecisionPanel";
import { cost, seconds, statusLabel } from "./format";

const REFRESH_MS = 3000;
const NODE_NAMES: Record<string, string> = {
  planner: "Planner",
  retriever: "Policy search",
  tool_agent: "Tool agent",
  critic: "Critic",
  approval_gate: "Approval gate",
  commit: "Commit",
};

export function RunView({ token, scope, runId }: { token: string; scope: Scope; runId: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  const [sent, setSent] = useState("");

  const load = useCallback(
    () =>
      api.run(token, runId).then(
        (next) => {
          setDetail(next);
          setError("");
          if (!next.pending) setSent("");
        },
        (e: Error) => setError(e.message),
      ),
    [token, runId],
  );

  useEffect(() => {
    load();
  }, [load]);

  // A decision is sent to the workflow, so keep reading until the worker has finished the run.
  useEffect(() => {
    if (!sent) return;
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [sent, load]);

  if (error && !detail) return <p role="alert">{error}</p>;
  if (!detail) return <p>Loading…</p>;
  const { run, pending, outcome } = detail;

  return (
    <>
      <a href="#/">← Back to queue</a>
      <div className="heading">
        <h1>{run.ticket_key ?? run.request_id}</h1>
        <span className={`status ${run.status ?? ""}`}>{statusLabel(run.status)}</span>
      </div>
      {error && <p role="alert">{error}</p>}

      <section>
        <h2>Customer request</h2>
        <p className="meta">{detail.customer_email ?? "Unknown customer"}. Untrusted text.</p>
        <p className="untrusted">{detail.request_text ?? "Not recorded."}</p>
      </section>

      {pending && (
        <DecisionPanel
          token={token}
          scope={scope}
          runId={runId}
          pending={pending}
          sent={sent}
          onSent={(decision) => {
            setSent(decision);
            load();
          }}
        />
      )}

      {outcome && (
        <section>
          <h2>Outcome</h2>
          <dl>
            <dt>Status</dt>
            <dd>{statusLabel(outcome.status)}</dd>
            <dt>Executed</dt>
            <dd>{outcome.executed ? "Yes" : "No"}</dd>
            {run.approved_by && (
              <>
                <dt>Decided by</dt>
                <dd>{run.approved_by}</dd>
              </>
            )}
            <dt>Arguments</dt>
            <dd>
              <code>{JSON.stringify(outcome.args)}</code>
            </dd>
            <dt>Reply</dt>
            <dd>{outcome.reply}</dd>
          </dl>
        </section>
      )}

      <section>
        <h2>Trace</h2>
        <p className="meta">
          {run.llm_calls ?? 0} LLM calls, {cost(run.cost_usd)}, {run.model ?? "unknown model"}.
          Langfuse trace <code>{detail.trace_id}</code>
        </p>
        {detail.steps.length === 0 && <p className="empty">No steps were saved for this run.</p>}
        <ol className="steps">
          {detail.steps.map((step) => (
            <li key={step.started_at}>
              <details>
                <summary>
                  <span>{NODE_NAMES[step.node] ?? step.node}</span>
                  <span className="meta">{seconds(step.seconds)}</span>
                </summary>
                <pre>{JSON.stringify(step.output, null, 2)}</pre>
              </details>
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
