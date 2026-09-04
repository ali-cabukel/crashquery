from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from crashquery.evaluation.harness import load_gold_cases, normalise, result_signature


def test_normalise_decimal_and_int() -> None:
    assert normalise(Decimal("12.5")) == 12.5
    assert normalise(42) == 42.0
    assert normalise(None) is None
    assert normalise(True) is True


def test_normalise_dates() -> None:
    assert normalise(date(2022, 1, 15)) == "2022-01-15"
    assert normalise(datetime(2022, 1, 15, 8, 30)) == "2022-01-15T08:30:00"


def test_normalise_float_rounding() -> None:
    assert normalise(1.0000004) == round(1.0000004, 6)


def test_result_signature_ignores_column_names_and_order() -> None:
    left = [{"n": 10, "label": "Fatal"}]
    right = [{"total": 10, "severity": "Fatal"}]
    assert result_signature(left) == result_signature(right)


def test_result_signature_ignores_row_order() -> None:
    left = [{"a": 1}, {"a": 2}]
    right = [{"a": 2}, {"a": 1}]
    assert result_signature(left) == result_signature(right)


def test_result_signature_mismatch() -> None:
    left = [{"n": 10}]
    right = [{"n": 11}]
    assert result_signature(left) != result_signature(right)


def test_gold_yaml_loads_via_package_resources() -> None:
    cases = load_gold_cases()
    ids = {c["id"] for c in cases}
    assert "fatal_casualties_2022" in ids
    assert "severity_trend_artefact" in ids
    assert all("question" in c for c in cases)
