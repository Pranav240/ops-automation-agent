"""Run history in Postgres: one row per agent or eval run, read by the text-to-SQL agent.

`agent_runs` is written by the app role. The SQL agent's role (`sql_reader`) can read only the
`run_history` view, which leaves out customer emails. `sql_query_log` records every SQL question.

Usage:
  python -m agent.run_history setup                       # tables, view, read-only role
  python -m agent.run_history load results/runs/*.jsonl   # import saved eval runs
"""

import argparse
import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql

from agent.config import db_url

READER_ROLE = "sql_reader"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id text PRIMARY KEY,
    source text NOT NULL,              -- jira | eval
    ticket_key text,
    request_id text,
    customer_email text,
    category text,
    system text NOT NULL,
    model text,
    git_commit text,
    run_date date NOT NULL DEFAULT current_date,
    status text,   -- done | pending_approval | rejected | blocked | failed | error
    action text,                       -- refund | change_status | escalate | close | NULL
    amount numeric,
    reason text,
    kind text,                         -- answer | clarification | refusal
    sent_to_approval boolean NOT NULL DEFAULT false,
    approved_by text,
    executed boolean NOT NULL DEFAULT false,
    revisions int,
    llm_calls int,
    cost_usd numeric,
    latency_s numeric
);
CREATE OR REPLACE VIEW run_history AS
    SELECT run_id, source, ticket_key, request_id, category, system, model, git_commit, run_date,
           status, action, amount, reason, kind, sent_to_approval, approved_by, executed,
           revisions, llm_calls, cost_usd, latency_s
    FROM agent_runs;
CREATE TABLE IF NOT EXISTS sql_query_log (
    id bigserial PRIMARY KEY,
    asked_at timestamptz NOT NULL DEFAULT now(),
    question text NOT NULL,
    sql text,
    verdict text NOT NULL,             -- ok | refused | rejected | error
    reason text,
    row_count int,
    cost_usd numeric
);
"""

COLUMNS = [
    "run_id", "source", "ticket_key", "request_id", "customer_email", "category", "system",
    "model", "git_commit", "run_date", "status", "action", "amount", "reason", "kind",
    "sent_to_approval", "approved_by", "executed", "revisions", "llm_calls", "cost_usd",
    "latency_s",
]  # fmt: skip


def setup(conninfo: str, reader_password: str) -> None:
    """Create the tables and view, and a login role that can only SELECT from run_history."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(SCHEMA)
        role = sql.Identifier(READER_ROLE)
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [READER_ROLE]).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(role, sql.Literal(reader_password))
        )
        db = sql.Identifier(conn.info.dbname)
        for stmt in (
            "ALTER ROLE {role} SET default_transaction_read_only = on",
            "ALTER ROLE {role} SET statement_timeout = '5s'",
            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}",
            "REVOKE CREATE ON SCHEMA public FROM {role}",
            "GRANT CONNECT ON DATABASE {db} TO {role}",
            "GRANT USAGE ON SCHEMA public TO {role}",
            "GRANT SELECT ON run_history TO {role}",
        ):
            conn.execute(sql.SQL(stmt).format(role=role, db=db))


def reader_url() -> str:
    """Connection string for the read-only role used by the SQL agent."""
    return (
        f"postgresql://{READER_ROLE}:{os.environ['SQL_READER_PASSWORD']}"
        f"@{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5433')}"
        f"/{os.environ['POSTGRES_DB']}"
    )


def from_record(rec: dict, source: str, **extra) -> dict:
    """Map an evals.runner record (or a worker record of the same shape) to a table row."""
    args = rec.get("args") or {}
    return {
        "run_id": rec.get("run_id")
        or f"{rec['system']}-{rec['commit']}-r{rec.get('repeat', 1)}-{rec['request_id']}",
        "source": source,
        "ticket_key": None,
        "request_id": rec.get("request_id"),
        "customer_email": None,
        "category": rec.get("category"),
        "system": rec["system"],
        "model": rec.get("model"),
        "git_commit": rec.get("commit"),
        "run_date": rec.get("date"),
        "status": rec.get("status"),
        "action": rec.get("action"),
        "amount": args.get("amount"),
        "reason": args.get("reason"),
        "kind": rec.get("kind") or rec.get("label"),
        "sent_to_approval": bool(rec.get("sent_to_approval")),
        "approved_by": None,
        "executed": bool(rec.get("executed")),
        "revisions": rec.get("revisions"),
        "llm_calls": rec.get("llm_calls"),
        "cost_usd": rec.get("cost_usd"),
        "latency_s": rec.get("latency_s"),
        **extra,
    }


class RunHistory:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def record(self, row: dict) -> None:
        """Insert a run, or update the given columns when the run_id exists (after a resume)."""
        row = {c: row[c] for c in COLUMNS if c in row and (c != "run_date" or row[c])}
        cols = list(row)
        query = sql.SQL(
            "INSERT INTO agent_runs ({cols}) VALUES ({vals}) "
            "ON CONFLICT (run_id) DO UPDATE SET {updates}"
        ).format(
            cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            vals=sql.SQL(", ").join(sql.Placeholder(c) for c in cols),
            updates=sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in cols[1:]
            ),
        )
        with psycopg.connect(self.conninfo) as conn:
            conn.execute(query, row)

    def ticket_for(self, run_id: str) -> str | None:
        with psycopg.connect(self.conninfo) as conn:
            found = conn.execute(
                "SELECT ticket_key FROM agent_runs WHERE run_id = %s", [run_id]
            ).fetchone()
        return found[0] if found else None


def load(conninfo: str, paths: list[Path]) -> int:
    history = RunHistory(conninfo)
    n = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("split") != "dev":
                continue  # the test split stays out of the database until Phase 8
            history.record(from_record(rec, "eval"))
            n += 1
    return n


if __name__ == "__main__":
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    p = sub.add_parser("load")
    p.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    if args.command == "setup":
        setup(db_url(), os.environ["SQL_READER_PASSWORD"])
        print(f"run history tables ready; role {READER_ROLE} can read run_history only")
    else:
        print(f"loaded {load(db_url(), args.files)} runs")
