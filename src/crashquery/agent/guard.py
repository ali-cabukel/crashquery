"""SQL safety guard.

Layered, because any single layer will eventually be bypassed:

  1. Postgres role       — rsa_agent cannot write. This is the real boundary.
  2. statement_timeout   — set on the role, kills runaway queries.
  3. This module         — parses the SQL and rejects what shouldn't run.
  4. EXPLAIN cost check  — refuses queries the planner thinks are enormous.

Layer 3 exists mainly to give the model a useful error message it can recover
from. Treating it as the security boundary would be a mistake: it is a parser,
and parsers have gaps. Never run the agent as a role that can write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

DIALECT = "postgres"

# Only these top-level statement types may execute.
ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# Expression types that must never appear anywhere in the tree.
#
# exp.Into catches `SELECT * INTO new_table FROM ...`, which parses as a Select
# — the root-type check passes it — but is CREATE TABLE AS in Postgres. Worth
# remembering that "it starts with SELECT" is not the same as "it only reads".
#
# Postgres also allows data-modifying CTEs (`WITH d AS (DELETE ... RETURNING *)
# SELECT * FROM d`). Those are caught because this tuple is checked against
# every node in the tree, not just the root.
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Merge,
    exp.Into,
)

# Functions that touch the filesystem, run commands, or leak connection state.
FORBIDDEN_FUNCTIONS = {
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "pg_sleep",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "query_to_xml",
    "pg_read_server_files",
    "copy",
}

# System schemas the agent has no business reading.
FORBIDDEN_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}

MAX_LIMIT = 1000
DEFAULT_LIMIT = 200
MAX_EXPLAIN_COST = 5_000_000.0


class UnsafeQuery(ValueError):
    """Raised when a query must not be executed."""


@dataclass
class GuardResult:
    sql: str  # the rewritten, safe-to-execute SQL
    limit_applied: bool
    notes: list[str]


def _reject(message: str) -> None:
    raise UnsafeQuery(message)


def check(query: str, max_limit: int = MAX_LIMIT) -> GuardResult:
    """Validate and normalise a generated query.

    Returns the SQL to actually execute, or raises UnsafeQuery with a message
    written for the model to read and retry against.
    """
    if not query or not query.strip():
        _reject("Empty query.")

    notes: list[str] = []

    try:
        statements = sqlglot.parse(query, dialect=DIALECT)
    except Exception as exc:
        _reject(f"Could not parse as PostgreSQL: {exc}")

    statements = [s for s in statements if s is not None]

    if len(statements) == 0:
        _reject("No statement found.")
    if len(statements) > 1:
        _reject(
            f"Found {len(statements)} statements. Submit exactly one SELECT — "
            "no semicolon-separated batches."
        )

    tree = statements[0]

    if not isinstance(tree, ALLOWED_ROOTS):
        _reject(
            f"Only SELECT queries are allowed; got {type(tree).__name__.upper()}. "
            "This database is read-only."
        )

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            _reject(f"{type(node).__name__.upper()} is not permitted.")

        if isinstance(node, exp.Anonymous):
            name = (node.this or "").lower()
            if name in FORBIDDEN_FUNCTIONS:
                _reject(f"Function {name}() is not permitted.")

        if isinstance(node, exp.Table):
            schema = (node.db or "").lower()
            if schema in FORBIDDEN_SCHEMAS:
                _reject(
                    f"Schema {schema} is not readable. Use the describe_table "
                    "tool for schema information."
                )
            table_name = (node.name or "").lower()
            if table_name.startswith("pg_"):
                _reject(f"System table {table_name} is not readable.")

    # --- enforce a row cap -------------------------------------------------
    # Without this an agent will cheerfully SELECT * from a million rows and
    # blow the model's context window, which fails in a far more confusing way
    # than a database error.
    #
    # Aggregates are the exception: a GROUP BY can legitimately return more
    # than DEFAULT_LIMIT groups, and silently clipping them breaks evaluation.
    # An explicit enormous LIMIT is still capped.
    limit_applied = False
    existing = tree.args.get("limit")

    if existing is None:
        if not is_aggregate_query(query):
            tree = tree.limit(DEFAULT_LIMIT)
            limit_applied = True
            notes.append(f"No LIMIT given; applied LIMIT {DEFAULT_LIMIT}.")
    else:
        try:
            value = int(existing.expression.this)
            if value > max_limit:
                tree = tree.limit(max_limit)
                limit_applied = True
                notes.append(f"LIMIT {value} reduced to {max_limit}.")
        except (AttributeError, TypeError, ValueError):
            notes.append("Could not read the LIMIT value; left as written.")

    return GuardResult(
        sql=tree.sql(dialect=DIALECT, pretty=True),
        limit_applied=limit_applied,
        notes=notes,
    )


def is_aggregate_query(query: str) -> bool:
    """True if the query aggregates — used to skip the LIMIT nudge in prompts."""
    try:
        tree = sqlglot.parse_one(query, dialect=DIALECT)
    except Exception:
        return False
    if tree.args.get("group"):
        return True
    return any(isinstance(node, exp.AggFunc) for node in tree.walk())


COST_PATTERN = re.compile(r"cost=[\d.]+\.\.([\d.]+)")


def parse_explain_cost(explain_output: str) -> float | None:
    """Pull the total cost off the root node of an EXPLAIN plan."""
    match = COST_PATTERN.search(explain_output)
    return float(match.group(1)) if match else None
