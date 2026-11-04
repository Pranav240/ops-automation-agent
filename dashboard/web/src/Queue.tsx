import { useEffect, useState } from "react";
import { api, type Run } from "./api";
import { cost, money, statusLabel, time } from "./format";

const REFRESH_MS = 5000;

export function Queue({ token }: { token: string }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [onlyPending, setOnlyPending] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = () =>
      api.runs(token, onlyPending ? "pending_approval" : undefined).then(
        (rows) => {
          if (active) {
            setRuns(rows);
            setError("");
          }
        },
        (e: Error) => active && setError(e.message),
      );
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [token, onlyPending]);

  return (
    <>
      <div className="heading">
        <h1>Request queue</h1>
        <label className="check">
          <input
            type="checkbox"
            checked={onlyPending}
            onChange={(event) => setOnlyPending(event.target.checked)}
          />
          Needs approval only
        </label>
      </div>
      {error && <p role="alert">{error}</p>}
      {runs === null && !error && <p>Loading…</p>}
      {runs?.length === 0 && <p className="empty">No requests to show.</p>}
      {runs && runs.length > 0 && (
        <div className="table">
          <table>
            <thead>
              <tr>
                <th>Request</th>
                <th>Status</th>
                <th>Action</th>
                <th className="number">Amount</th>
                <th className="number">LLM cost</th>
                <th>Received</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td>
                    <a href={`#/runs/${run.run_id}`}>{run.ticket_key ?? run.request_id}</a>
                  </td>
                  <td>
                    <span className={`status ${run.status ?? ""}`}>{statusLabel(run.status)}</span>
                  </td>
                  <td>{run.action ?? "No action"}</td>
                  <td className="number">{money(run.amount)}</td>
                  <td className="number">{cost(run.cost_usd)}</td>
                  <td>{time(run.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
