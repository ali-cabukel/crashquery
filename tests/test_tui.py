from crashquery.tui.app import CrashqueryApp
from crashquery.tui.formatters import describe_markdown, last_sql, table_rows, tools_markup


def test_table_rows_format_counts() -> None:
    rows = table_rows(
        [
            {"table_name": "collisions", "approx_rows": 311349},
            {"table_name": "empty", "approx_rows": 0},
        ]
    )
    assert rows[0] == ("collisions", "~311,349")
    assert rows[1] == ("empty", "unknown")


def test_last_sql_takes_final_nonempty() -> None:
    assert last_sql(["SELECT 1", "", "SELECT count(*) FROM collisions"]) == (
        "SELECT count(*) FROM collisions"
    )
    assert last_sql([]) == ""


def test_tools_markup() -> None:
    assert "no tools" in tools_markup([])
    assert "lookup_codes" in tools_markup([{"tool": "lookup_codes"}])


def test_describe_markdown_includes_coded_columns() -> None:
    text = describe_markdown(
        {
            "table": "casualties",
            "description": "One row per person injured.",
            "columns": [
                {
                    "column_name": "casualty_severity",
                    "data_type": "smallint",
                    "is_nullable": True,
                }
            ],
            "coded_columns": ["casualty_severity"],
        }
    )
    assert "## casualties" in text
    assert "casualty_severity" in text
    assert "Coded columns" in text


def test_app_title() -> None:
    assert CrashqueryApp.TITLE == "crashquery"
