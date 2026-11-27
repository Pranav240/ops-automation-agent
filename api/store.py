"""What the HTTP API keeps in Postgres, next to agent_runs and the checkpoints.

api_runs: one row per request_id (so the same request never starts two runs) with its API status.
api_decisions: one row per decision_id (so the same decision never resumes a run twice).

A run leaves "paused" in the same transaction that stores its decision, guarded by
WHERE status = 'paused', so two different decisions sent at once cannot both resume it.
"""

from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_runs (
    run_id text PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    tenant_id text NOT NULL,
    customer_email text NOT NULL,
    request_text text NOT NULL,
    status text NOT NULL,      -- queued | running | paused | committed | rejected | failed
    result jsonb,              -- the agent's latest result (agent.graph.start / resume)
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS api_decisions (
    decision_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES api_runs (run_id),
    decision text NOT NULL,    -- approve | edit | reject
    args jsonb,                -- the full edited arguments, for an edit
    decided_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

RUN_QUERY = """
SELECT a.run_id, a.request_id, a.tenant_id, a.customer_email, a.request_text, a.status,
       a.result, a.error, a.created_at, a.updated_at, h.cost_usd,
       (SELECT d.decided_by FROM api_decisions d WHERE d.run_id = a.run_id
        ORDER BY d.created_at DESC LIMIT 1) AS decided_by
FROM api_runs a LEFT JOIN agent_runs h ON h.run_id = a.run_id
"""


class NotPaused(Exception):
    pass


def clean(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


class ApiStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def connect(self):
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def setup(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)

    def create_run(self, run_id: str, request_id: str, tenant_id: str, email: str, text: str):
        """(the run for this request_id, True if it was created now)."""
        with self.connect() as conn:
            created = conn.execute(
                "INSERT INTO api_runs (run_id, request_id, tenant_id, customer_email, "
                "request_text, status) VALUES (%s, %s, %s, %s, %s, 'queued') "
                "ON CONFLICT (request_id) DO NOTHING RETURNING run_id",
                [run_id, request_id, tenant_id, email, text],
            ).fetchone()
            row = conn.execute(RUN_QUERY + " WHERE a.request_id = %s", [request_id]).fetchone()
        return clean(row), created is not None

    def get(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            return clean(conn.execute(RUN_QUERY + " WHERE a.run_id = %s", [run_id]).fetchone())

    def set_status(
        self, run_id: str, status: str, expect: str, result: dict | None = None, error: str = ""
    ) -> bool:
        """Move the run from `expect` to `status`. False if it was not in `expect`."""
        with self.connect() as conn:
            moved = conn.execute(
                "UPDATE api_runs SET status = %s, result = COALESCE(%s, result), "
                "error = NULLIF(%s, ''), updated_at = now() "
                "WHERE run_id = %s AND status = %s RETURNING run_id",
                [status, Jsonb(result) if result is not None else None, error, run_id, expect],
            ).fetchone()
        return moved is not None

    def decision(self, decision_id: str) -> dict | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM api_decisions WHERE decision_id = %s", [decision_id]
            ).fetchone()

    def add_decision(
        self, decision_id: str, run_id: str, decision: str, args: dict | None, decided_by: str
    ) -> bool:
        """Store the decision and move the run to running. False if decision_id was already used.

        Raises NotPaused (and stores nothing) when the run is not paused.
        """
        with self.connect() as conn:
            stored = conn.execute(
                "INSERT INTO api_decisions (decision_id, run_id, decision, args, decided_by) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (decision_id) DO NOTHING "
                "RETURNING decision_id",
                [decision_id, run_id, decision, Jsonb(args) if args else None, decided_by],
            ).fetchone()
            if stored is None:
                return False
            moved = conn.execute(
                "UPDATE api_runs SET status = 'running', updated_at = now() "
                "WHERE run_id = %s AND status = 'paused' RETURNING run_id",
                [run_id],
            ).fetchone()
            if moved is None:
                conn.rollback()
                raise NotPaused(run_id)
        return True

    def unfinished(self) -> list[dict]:
        """Runs that were queued or running, e.g. when the API process stopped."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT run_id, status FROM api_runs WHERE status IN ('queued', 'running')"
            ).fetchall()
