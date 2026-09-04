from __future__ import annotations

import pytest

from crashquery.agent.guard import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    UnsafeQuery,
    check,
    is_aggregate_query,
    parse_explain_cost,
)


def test_empty_query_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="Empty query"):
        check("")
    with pytest.raises(UnsafeQuery, match="Empty query"):
        check("   ")


def test_non_select_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="Only SELECT"):
        check("DELETE FROM collisions")
    with pytest.raises(UnsafeQuery, match="Only SELECT"):
        check("UPDATE collisions SET speed_limit = 0")


def test_multiple_statements_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="exactly one SELECT"):
        check("SELECT 1; SELECT 2")


def test_select_into_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="INTO"):
        check("SELECT * INTO evil FROM collisions")


def test_modifying_cte_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="DELETE"):
        check("WITH d AS (DELETE FROM collisions RETURNING *) SELECT * FROM d")


def test_forbidden_schema_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="information_schema"):
        check("SELECT * FROM information_schema.tables")


def test_system_table_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="System table"):
        check("SELECT * FROM pg_class")


def test_auto_limit_on_non_aggregate() -> None:
    result = check("SELECT * FROM collisions")
    assert result.limit_applied
    assert f"LIMIT {DEFAULT_LIMIT}" in result.sql
    assert any("applied LIMIT" in n for n in result.notes)


def test_no_auto_limit_on_count() -> None:
    result = check("SELECT count(*) AS n FROM casualties WHERE accident_year = 2022")
    assert not result.limit_applied
    assert "LIMIT" not in result.sql.upper()


def test_no_auto_limit_on_group_by() -> None:
    result = check(
        "SELECT casualty_severity, count(*) AS n FROM casualties GROUP BY casualty_severity"
    )
    assert not result.limit_applied
    assert "LIMIT" not in result.sql.upper()


def test_enormous_limit_still_capped() -> None:
    result = check("SELECT casualty_severity, count(*) FROM casualties GROUP BY 1 LIMIT 50000")
    assert result.limit_applied
    assert f"LIMIT {MAX_LIMIT}" in result.sql


def test_is_aggregate_query() -> None:
    assert is_aggregate_query("SELECT count(*) FROM collisions")
    assert is_aggregate_query("SELECT road_type, count(*) FROM collisions GROUP BY road_type")
    assert not is_aggregate_query("SELECT * FROM collisions")
    assert not is_aggregate_query(
        "SELECT accident_index FROM collisions WHERE accident_year = 2022"
    )


def test_parse_explain_cost() -> None:
    plan = "Seq Scan on collisions  (cost=0.00..12345.67 rows=100 width=8)"
    assert parse_explain_cost(plan) == 12345.67
    assert parse_explain_cost("no cost here") is None
