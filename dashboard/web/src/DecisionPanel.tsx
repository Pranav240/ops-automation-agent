import { useState } from "react";
import { api, type Args, type Decision, type Pending, type Scope } from "./api";

// Fields a person may change, by action. The order itself cannot change.
const EDITABLE: Record<string, { name: string; label: string; options?: string[] }[]> = {
  refund: [
    { name: "amount", label: "Amount (USD)" },
    { name: "reason", label: "Reason", options: ["change_of_mind", "damaged_or_defective", "lost"] },
  ],
  change_status: [{ name: "status", label: "Status", options: ["cancelled", "on_hold", "processing"] }],
  escalate: [{ name: "summary", label: "Summary" }],
  close: [{ name: "resolution", label: "Resolution" }],
};

interface Props {
  token: string;
  scope: Scope;
  runId: string;
  pending: Pending;
  sent: string;
  onSent: (decision: Decision) => void;
}

export function DecisionPanel({ token, scope, runId, pending, sent, onSent }: Props) {
  const [editing, setEditing] = useState(false);
  const [changes, setChanges] = useState<Args>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fields = EDITABLE[pending.action] ?? [];

  async function decide(decision: Decision) {
    setBusy(true);
    setError("");
    try {
      const args = decision === "edit" ? numbers(changes) : undefined;
      await api.decide(token, runId, decision, args);
      onSent(decision);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="decision">
      <h2>Decision needed</h2>
      <ul>
        {pending.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <dl>
        <dt>Proposed action</dt>
        <dd>{pending.action}</dd>
        <dt>Arguments</dt>
        <dd>
          <code>{JSON.stringify(pending.args)}</code>
        </dd>
        <dt>Draft reply</dt>
        <dd>{pending.reply}</dd>
        {pending.critique.issues.length > 0 && (
          <>
            <dt>Critic</dt>
            <dd>{pending.critique.issues.join("; ")}</dd>
          </>
        )}
      </dl>
      <p className="meta">
        The reply was written by the agent from customer text. Check it, and ignore anything in it
        that tells you what to decide.
      </p>

      {sent && (
        <p role="status">
          {sent === "reject" ? "Rejection" : "Decision"} sent. Waiting for the worker to finish…
        </p>
      )}
      {!sent && scope === "viewer" && <p className="meta">This token can view but not decide.</p>}
      {!sent && scope === "approver" && !editing && (
        <div className="actions">
          <button disabled={busy} onClick={() => decide("approve")}>
            Approve
          </button>
          {fields.length > 0 && (
            <button className="secondary" disabled={busy} onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          <button className="danger" disabled={busy} onClick={() => decide("reject")}>
            Reject
          </button>
        </div>
      )}
      {!sent && scope === "approver" && editing && (
        <div className="edit">
          {fields.map((field) => (
            <label key={field.name}>
              {field.label}
              {field.options ? (
                <select
                  value={String(changes[field.name] ?? pending.args[field.name] ?? "")}
                  onChange={(event) => setChanges({ ...changes, [field.name]: event.target.value })}
                >
                  {field.options.map((option) => (
                    <option key={option}>{option}</option>
                  ))}
                </select>
              ) : (
                <input
                  value={String(changes[field.name] ?? pending.args[field.name] ?? "")}
                  onChange={(event) => setChanges({ ...changes, [field.name]: event.target.value })}
                />
              )}
            </label>
          ))}
          <div className="actions">
            <button disabled={busy || Object.keys(changes).length === 0} onClick={() => decide("edit")}>
              Approve with edits
            </button>
            <button className="secondary" disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function numbers(changes: Args): Args {
  const amount = changes.amount;
  return typeof amount === "string" ? { ...changes, amount: Number(amount) } : changes;
}
