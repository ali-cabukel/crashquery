"""Tools exposed to the agent.

Design notes worth arguing about in an interview:

* Schema introspection is a *tool*, not a prompt dump. Pasting a 60-column
  schema into the system prompt burns context on every turn and gets worse as
  the schema grows. Making the model ask means it only pays for what it needs.

* `lookup_codes` is the tool that makes this schema tractable. STATS19 columns
  are integers whose meaning lives elsewhere; without this the model guesses,
  and it guesses plausibly enough that the error is hard to spot.

* Every tool returns a string. Errors come back as readable text rather than
  raised exceptions, so the model can correct itself instead of the run dying.
"""

from __future__ import annotations

import logging

import psycopg
from langchain_core.tools import tool
from psycopg import sql as pgsql

from crashquery.agent import db
from crashquery.agent.guard import UnsafeQuery, check

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 6000


def _truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated at {limit} characters — aggregate or filter]"


def _sql(template: str, **parts: pgsql.Composable) -> str:
    return pgsql.SQL(template).format(**parts).as_string()


@tool
def list_tables() -> str:
    """List the tables available, with row counts and grain descriptions.

    Always call this first. The descriptions explain the grain of each table,
    which determines whether a COUNT means collisions, vehicles or people.
    """
    try:
        tables = db.list_tables()
    except psycopg.Error as exc:
        return f"Database error: {exc}"

    lines = []
    for row in tables:
        rows_estimate = row["approx_rows"]
        estimate = f"~{rows_estimate:,}" if rows_estimate and rows_estimate > 0 else "unknown"
        lines.append(f"### {row['table_name']}  ({estimate} rows)")
        if row["description"]:
            lines.append(row["description"])
        lines.append("")
    return "\n".join(lines) or "No tables found."


@tool
def describe_table(table: str) -> str:
    """Show columns, types and descriptions for one table.

    Also lists which columns are coded integers needing lookup_codes.

    Args:
        table: Table name, e.g. 'collisions', 'casualties', 'vehicles'.
    """
    try:
        info = db.describe_table(table)
    except psycopg.Error as exc:
        return f"Database error: {exc}"

    if not info["columns"]:
        available = ", ".join(t["table_name"] for t in db.list_tables())
        return f"No table named '{table}'. Available: {available}"

    lines = [f"## {table}"]
    if info["description"]:
        lines.append(info["description"])
    lines.append("\n| column | type | nullable |")
    lines.append("| --- | --- | --- |")
    for col in info["columns"]:
        lines.append(
            f"| {col['column_name']} | {col['data_type']} | "
            f"{'yes' if col['is_nullable'] else 'no'} |"
        )

    if info["coded_columns"]:
        lines.append(
            "\n**Coded columns** (integers — call lookup_codes before "
            "filtering or interpreting): " + ", ".join(info["coded_columns"])
        )
    lines.append(
        "\nNote: -1 means 'data missing or out of range' throughout STATS19. "
        "Exclude it explicitly rather than letting it fall into a numeric "
        "aggregate."
    )
    return _truncate("\n".join(lines))


@tool
def lookup_codes(field: str, table: str = "") -> str:
    """Decode a coded column: what each integer value means.

    Essential before filtering on any categorical column. For example
    casualty_severity = 1 means Fatal, not Slight.

    Args:
        field: Column name, e.g. 'casualty_severity', 'weather_conditions'.
        table: Optional table name to disambiguate.
    """
    try:
        rows = db.lookup_codes(field, table or None)
    except psycopg.Error as exc:
        return f"Database error: {exc}"

    if not rows:
        similar = db.search_coded_fields(field)
        if similar:
            names = ", ".join(sorted({r["field_name"] for r in similar}))
            return f"No codes for '{field}'. Did you mean: {names}?"
        return (
            f"No code dictionary entry for '{field}'. It may be a genuine "
            "numeric column (e.g. speed_limit, age_of_casualty) rather than a "
            "coded one — check describe_table."
        )

    lines = [f"Codes for {field}:"]
    lines += [f"  {r['code']:>4} = {r['label']}" for r in rows]
    return "\n".join(lines)


@tool
def find_coded_field(pattern: str) -> str:
    """Search for coded columns by partial name.

    Use when you know the concept but not the exact column name, e.g.
    'weather', 'severity', 'age'.

    Args:
        pattern: Substring to search for in column names.
    """
    try:
        rows = db.search_coded_fields(pattern)
    except psycopg.Error as exc:
        return f"Database error: {exc}"
    if not rows:
        return f"No coded columns matching '{pattern}'."
    return "\n".join(f"{r['table_name']}.{r['field_name']}" for r in rows)


@tool
def validate_sql(query: str) -> str:
    """Check a query for safety and get its estimated cost, without running it.

    Use this on any query you expect to be expensive, before run_sql.

    Args:
        query: A single PostgreSQL SELECT statement.
    """
    try:
        guarded = check(query)
    except UnsafeQuery as exc:
        return f"REJECTED: {exc}"

    try:
        plan, cost = db.explain(guarded.sql)
    except psycopg.Error as exc:
        return f"Query is well-formed but failed to plan: {exc}"

    parts = [f"OK. Estimated cost: {cost:,.0f}" if cost else "OK."]
    if guarded.notes:
        parts.append("Notes: " + "; ".join(guarded.notes))
    parts.append(f"\nPlan:\n{plan}")
    return _truncate("\n".join(parts), 2000)


@tool
def run_sql(query: str) -> str:
    """Execute a read-only SELECT and return the rows.

    The database is read-only and a row limit is applied automatically.
    Prefer aggregating in SQL over selecting raw rows.

    Args:
        query: A single PostgreSQL SELECT statement.
    """
    try:
        result = db.run_query(query)
    except UnsafeQuery as exc:
        return f"REJECTED: {exc}"
    except psycopg.Error as exc:
        # Give the model the error verbatim — Postgres messages name the
        # offending column, which is usually enough for it to self-correct.
        return f"SQL error: {exc}"

    header = f"{result.row_count} row(s)."
    if result.cost:
        header += f" Plan cost {result.cost:,.0f}."
    return _truncate(f"{header}\n\n{result.to_markdown()}")


@tool
def profile_column(table: str, column: str) -> str:
    """Profile one column: nulls, distinct count, distribution, range.

    The fastest way to understand a column before using it. For coded columns
    the value counts are joined to their labels automatically.

    Args:
        table: Table name.
        column: Column name to profile.
    """
    info = db.describe_table(table)
    names = {c["column_name"] for c in info["columns"]}
    if column not in names:
        return f"No column '{column}' in {table}. Columns: {', '.join(sorted(names))}"

    data_type = next(c["data_type"] for c in info["columns"] if c["column_name"] == column)
    is_numeric = any(t in data_type for t in ("int", "numeric", "double", "real"))

    ident_col = pgsql.Identifier(column)
    ident_table = pgsql.Identifier(table)
    field_literal = pgsql.Literal(column)

    try:
        summary = db.run_query(
            _sql(
                """
                SELECT count(*)                              AS total_rows,
                       count({col})                          AS non_null,
                       count(*) - count({col})               AS nulls,
                       count(DISTINCT {col})                 AS distinct_values
                FROM {tbl}
                """,
                col=ident_col,
                tbl=ident_table,
            ),
            enforce_cost_limit=False,
        )
        lines = [f"## {table}.{column}  ({data_type})", summary.to_markdown()]

        if is_numeric:
            stats = db.run_query(
                _sql(
                    """
                    SELECT min({col})                  AS minimum,
                           max({col})                  AS maximum,
                           round(avg({col})::numeric, 2) AS mean,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY {col})
                                                        AS median
                    FROM {tbl} WHERE {col} IS NOT NULL AND {col} <> -1
                    """,
                    col=ident_col,
                    tbl=ident_table,
                ),
                enforce_cost_limit=False,
            )
            lines += ["\n**Range** (excluding the -1 missing code):", stats.to_markdown()]

        if is_numeric:
            top_sql = _sql(
                """
                SELECT t.{col} AS value,
                       l.label,
                       count(*)     AS n,
                       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
                FROM {tbl} t
                LEFT JOIN code_lookups l
                       ON l.field_name = {field}
                      AND l.code = t.{col}
                GROUP BY t.{col}, l.label
                ORDER BY n DESC
                LIMIT 20
                """,
                col=ident_col,
                tbl=ident_table,
                field=field_literal,
            )
        else:
            top_sql = _sql(
                """
                SELECT {col} AS value, NULL AS label, count(*) AS n,
                       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
                FROM {tbl} GROUP BY {col} ORDER BY n DESC LIMIT 20
                """,
                col=ident_col,
                tbl=ident_table,
            )
        top = db.run_query(top_sql, enforce_cost_limit=False)
        lines += ["\n**Most common values:**", top.to_markdown()]

    except (psycopg.Error, UnsafeQuery) as exc:
        return f"Profiling failed: {exc}"

    return _truncate("\n".join(lines))


@tool
def build_ml_dataset(
    target: str = "casualty_severity",
    years: str = "",
    limit: int = 50000,
) -> str:
    """Assemble a modelling dataset by joining collisions, vehicles, casualties.

    Returns the generated SQL plus class balance, ready to hand to scikit-learn.
    Handles the join grain correctly: one row per casualty, with the collision
    and vehicle context attached.

    Args:
        target: Target column. Currently 'casualty_severity'.
        years: Optional comma-separated years, e.g. '2021,2022'.
        limit: Maximum rows to sample.
    """
    if target != "casualty_severity":
        return "Only 'casualty_severity' is supported as a target so far."

    where = [
        "cas.casualty_severity <> -1",
        "col.collision_date IS NOT NULL",
    ]
    if years.strip():
        try:
            parsed = [str(int(y.strip())) for y in years.split(",") if y.strip()]
            where.append(f"col.accident_year IN ({', '.join(parsed)})")
        except ValueError:
            return f"Could not parse years: {years!r}. Use e.g. '2021,2022'."

    query = f"""
        SELECT
            cas.casualty_severity                    AS target,
            cas.casualty_class,
            cas.sex_of_casualty,
            NULLIF(cas.age_of_casualty, -1)          AS age_of_casualty,
            col.speed_limit,
            col.light_conditions,
            col.weather_conditions,
            col.road_surface_conditions,
            col.road_type,
            col.junction_detail,
            col.urban_or_rural_area,
            col.number_of_vehicles,
            col.number_of_casualties,
            EXTRACT(HOUR FROM col.time::time)        AS hour_of_day,
            col.day_of_week,
            veh.vehicle_type,
            NULLIF(veh.age_of_vehicle, -1)           AS age_of_vehicle
        FROM casualties cas
        JOIN collisions col ON col.accident_index = cas.accident_index
        LEFT JOIN vehicles veh
               ON veh.accident_index = cas.accident_index
              AND veh.vehicle_reference = cas.vehicle_reference
        WHERE {' AND '.join(where)}
        LIMIT {min(int(limit), 200000)}
    """

    try:
        balance = db.run_query(
            """
            SELECT l.label AS severity, count(*) AS n,
                   round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
            FROM casualties cas
            LEFT JOIN code_lookups l
                   ON l.field_name = 'casualty_severity'
                  AND l.code = cas.casualty_severity
            WHERE cas.casualty_severity <> -1
            GROUP BY l.label ORDER BY n DESC
            """,
            enforce_cost_limit=False,
        )
    except (psycopg.Error, UnsafeQuery) as exc:
        return f"Could not compute class balance: {exc}"

    return (
        "Dataset query:\n```sql\n"
        + query.strip()
        + "\n```\n\n**Class balance for the target:**\n"
        + balance.to_markdown()
        + "\n\nThis target is heavily imbalanced — fatal casualties are a small "
        "fraction of the total. Report precision, recall and PR-AUC for the "
        "fatal class; overall accuracy will look excellent and mean nothing.\n\n"
        "Also note: severity is not measured consistently across the whole "
        "series. Several police forces moved to injury-based reporting systems "
        "part-way through, which shifted the serious/slight split. Check DfT's "
        "severity adjustment guidance before comparing years."
    )


ALL_TOOLS = [
    list_tables,
    describe_table,
    lookup_codes,
    find_coded_field,
    validate_sql,
    run_sql,
    profile_column,
    build_ml_dataset,
]
