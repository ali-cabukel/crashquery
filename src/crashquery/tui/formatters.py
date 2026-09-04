"""Presentation helpers for the TUI — kept free of Textual so they can be tested."""

from __future__ import annotations

from typing import Any


def table_rows(tables: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for row in tables:
        count = row.get("approx_rows")
        estimate = f"~{count:,}" if count and count > 0 else "unknown"
        rows.append((str(row["table_name"]), estimate))
    return rows


def describe_markdown(info: dict[str, Any]) -> str:
    lines = [f"## {info['table']}"]
    if info.get("description"):
        lines.append(info["description"])
    lines += ["", "| column | type | nullable |", "| --- | --- | --- |"]
    for col in info.get("columns", []):
        lines.append(
            f"| {col['column_name']} | {col['data_type']} | "
            f"{'yes' if col['is_nullable'] else 'no'} |"
        )
    coded = info.get("coded_columns") or []
    if coded:
        lines += ["", f"Coded columns: {', '.join(coded)}"]
    return "\n".join(lines)


def tools_markup(trace: list[dict[str, Any]]) -> str:
    if not trace:
        return "[dim]no tools yet[/]"
    return "\n".join(f"[cyan]•[/] {call['tool']}" for call in trace)


def last_sql(sql_executed: list[str]) -> str:
    for query in reversed(sql_executed):
        if query and query.strip():
            return query.strip()
    return ""
