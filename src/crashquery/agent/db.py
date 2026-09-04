"""Database access for the agent.

Everything here connects as `rsa_agent`, the read-only role. There is no code
path in this package that can obtain a writable connection — that is deliberate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from crashquery.agent.guard import MAX_EXPLAIN_COST, UnsafeQuery, check, parse_explain_cost
from crashquery.settings import get_settings

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    notes: list[str]
    cost: float | None = None

    def to_markdown(self, max_rows: int = 50) -> str:
        if not self.rows:
            return "Query returned no rows."

        shown = self.rows[:max_rows]
        header = "| " + " | ".join(self.columns) + " |"
        divider = "| " + " | ".join("---" for _ in self.columns) + " |"
        body = [
            "| "
            + " | ".join(
                "NULL" if row.get(c) is None else str(row.get(c)) for c in self.columns
            )
            + " |"
            for row in shown
        ]

        parts = [header, divider, *body]
        if len(self.rows) > max_rows:
            parts.append(f"\n_({len(self.rows) - max_rows} further rows not shown)_")
        if self.notes:
            parts.append("\n" + "\n".join(f"_Note: {n}_" for n in self.notes))
        return "\n".join(parts)


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    settings = get_settings()
    conn = psycopg.connect(settings.agent_dsn, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {settings.statement_timeout_ms}")
            # Redundant with the role setting, but a role can be altered and
            # this cannot be reached without going through here.
            cur.execute("SET default_transaction_read_only = on")
        yield conn
    finally:
        conn.close()


def explain(
    query: str, params: Sequence[Any] | None = None
) -> tuple[str, float | None]:
    """Get the plan and estimated cost without running anything."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(f"EXPLAIN {query}", params)
        plan = "\n".join(row["QUERY PLAN"] for row in cur.fetchall())
    return plan, parse_explain_cost(plan)


def run_query(
    query: str,
    params: Sequence[Any] | None = None,
    *,
    enforce_cost_limit: bool = True,
    max_cost: float = MAX_EXPLAIN_COST,
) -> QueryResult:
    """Validate, cost-check, then execute.

    Raises UnsafeQuery for anything rejected before execution. Database errors
    propagate as psycopg exceptions — the tool layer turns those into messages
    the model can act on.
    """
    guarded = check(query)
    notes = list(guarded.notes)

    cost: float | None = None
    if enforce_cost_limit:
        try:
            _, cost = explain(guarded.sql, params)
        except psycopg.Error as exc:
            # An EXPLAIN failure is a genuine SQL error worth surfacing early,
            # before we spend time executing.
            raise UnsafeQuery(f"Query failed to plan: {exc}") from exc

        if cost is not None and cost > max_cost:
            raise UnsafeQuery(
                f"Estimated cost {cost:,.0f} exceeds the limit of {max_cost:,.0f}. "
                "Add a filter on accident_year or collision_date, or aggregate "
                "rather than selecting raw rows."
            )

    with connection() as conn, conn.cursor() as cur:
        cur.execute(guarded.sql, params)
        rows = cur.fetchall()
        columns = [d.name for d in cur.description] if cur.description else []

    return QueryResult(
        sql=guarded.sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=guarded.limit_applied,
        notes=notes,
        cost=cost,
    )


# --------------------------------------------------------------------------
# Schema introspection
# --------------------------------------------------------------------------
# These read the catalog directly rather than through the guard, because the
# guard blocks information_schema for generated SQL. That's the point: schema
# access is a curated tool, not something the model queries freely. It keeps
# the model's context small and stops it inventing catalog queries.


def list_tables() -> list[dict[str, Any]]:
    query = """
        SELECT c.relname AS table_name,
               obj_description(c.oid) AS description,
               c.reltuples::BIGINT     AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm')
        ORDER BY c.relname
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()


def describe_table(table: str) -> dict[str, Any]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname                                   AS column_name,
                   format_type(a.atttypid, a.atttypmod)        AS data_type,
                   NOT a.attnotnull                            AS is_nullable,
                   col_description(a.attrelid, a.attnum)       AS description
            FROM pg_attribute a
            JOIN pg_class c     ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (table,),
        )
        columns = cur.fetchall()

        cur.execute(
            "SELECT obj_description(c.oid) AS description FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=%s",
            (table,),
        )
        row = cur.fetchone()

        # Which of these columns have a code dictionary? Telling the model up
        # front is what stops it inventing meanings for integer categoricals.
        cur.execute(
            "SELECT DISTINCT field_name FROM code_lookups "
            "WHERE table_name = %s OR table_name = '*'",
            (table,),
        )
        coded = sorted(r["field_name"] for r in cur.fetchall() if r["field_name"] != "*")

    return {
        "table": table,
        "description": row["description"] if row else None,
        "columns": columns,
        "coded_columns": coded,
    }


def lookup_codes(field: str, table: str | None = None) -> list[dict[str, Any]]:
    sql_text = """
        SELECT table_name, field_name, code, label
        FROM code_lookups
        WHERE field_name = %s OR field_name = '*'
    """
    params: list[Any] = [field]
    if table:
        sql_text += " AND (table_name = %s OR table_name = '*')"
        params.append(table)
    sql_text += " ORDER BY code"

    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql_text, params)
        return cur.fetchall()


def search_coded_fields(pattern: str) -> list[dict[str, Any]]:
    """Find coded fields by fuzzy name — 'weather', 'severity', 'light'."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT table_name, field_name FROM code_lookups "
            "WHERE field_name ILIKE %s AND field_name <> '*' "
            "ORDER BY table_name, field_name",
            (f"%{pattern}%",),
        )
        return cur.fetchall()
