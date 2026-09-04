"""Evaluation harness.

Scores generated SQL by execution match: run the agent's query and the
reference query, compare the result sets. String comparison would fail every
correct rewrite, and LLM-judging the SQL is slower, costlier and less reliable
than just running it.

Two things are scored:

  * correctness — do the result sets match, order-insensitively?
  * behaviour   — did the agent consult the code dictionary when it needed to?

The second matters because a correct answer reached without checking the codes
is a coin flip that happened to land right, and it will land wrong on the next
coded column.

Rubric cases are printed for manual review rather than auto-scored. Automating
them with a judge model is a reasonable next step, but hand-reading twenty
traces teaches you more about your agent than any aggregate score.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from crashquery.agent.db import run_query
from crashquery.agent.graph import ask, build_agent

log = logging.getLogger(__name__)


def gold_path():
    return files("crashquery.evaluation").joinpath("gold.yaml")


def load_gold_cases() -> list[dict[str, Any]]:
    return yaml.safe_load(gold_path().read_text(encoding="utf-8"))["cases"]


def normalise(value: Any) -> Any:
    """Make values comparable across type differences that don't matter.

    Postgres returns Decimal for numeric and int for bigint; an agent writing
    `count(*)::float` isn't wrong. Rounding floats avoids failing a case over
    the last binary digit.
    """
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bool):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None:
        return None
    if isinstance(value, int):
        return float(value)
    return str(value)


def result_signature(rows: list[dict[str, Any]]) -> set[tuple]:
    """Order-insensitive, column-name-insensitive fingerprint of a result set.

    Column names are dropped deliberately: `AS n` versus `AS total` is not a
    correctness difference. Values within a row are sorted so column ORDER
    doesn't matter either.
    """
    return {tuple(sorted(str(normalise(v)) for v in row.values())) for row in rows}


def score_execution(agent_sql: str, reference_sql: str) -> tuple[bool, str]:
    if not agent_sql:
        return False, "agent ran no SQL"
    try:
        expected = run_query(reference_sql, enforce_cost_limit=False)
    except Exception as exc:
        return False, f"reference query failed: {exc}"
    try:
        actual = run_query(agent_sql, enforce_cost_limit=False)
    except Exception as exc:
        return False, f"agent query failed: {exc}"

    if result_signature(actual.rows) == result_signature(expected.rows):
        return True, "exact match"
    return (
        False,
        f"mismatch — expected {len(expected.rows)} row(s) "
        f"{result_signature(expected.rows)}, got {len(actual.rows)} row(s) "
        f"{result_signature(actual.rows)}",
    )


def run_case(agent, case: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    outcome = ask(agent, case["question"], thread_id=case["id"])
    elapsed = time.time() - started

    record: dict[str, Any] = {
        "id": case["id"],
        "question": case["question"],
        "answer": outcome["answer"],
        "tool_calls": outcome["tool_calls"],
        "tools_used": [t["tool"] for t in outcome["trace"]],
        "sql": outcome["sql_executed"][-1] if outcome["sql_executed"] else "",
        "seconds": round(elapsed, 1),
        "scoring": case.get("scoring", "execution"),
    }

    if case.get("scoring") == "rubric":
        record["rubric"] = case.get("rubric", [])
        record["passed"] = None  # manual
    else:
        passed, detail = score_execution(record["sql"], case["sql"])
        record["passed"] = passed
        record["detail"] = detail

    required = set(case.get("must_call", []))
    missing = required - set(record["tools_used"])
    record["behaviour_passed"] = not missing
    if missing:
        record["behaviour_detail"] = f"never called: {', '.join(sorted(missing))}"

    return record


def run_eval(
    *,
    model: str | None = None,
    only: str | None = None,
    out: Path = Path("eval/results.json"),
) -> int:
    cases = load_gold_cases()
    if only:
        cases = [c for c in cases if c["id"] == only]
        if not cases:
            print(f"No case with id {only!r}")
            return 1

    agent = build_agent(model) if model else build_agent()

    results = []
    for case in cases:
        print(f"\n{'=' * 72}\n{case['id']}: {case['question'][:70]}")
        record = run_case(agent, case)
        results.append(record)

        if record["scoring"] == "rubric":
            print("  RUBRIC — review manually")
            for item in record["rubric"]:
                print(f"    [ ] {item}")
        else:
            print(f"  {'PASS' if record['passed'] else 'FAIL'}  {record.get('detail', '')}")

        if not record["behaviour_passed"]:
            print(f"  BEHAVIOUR FAIL — {record['behaviour_detail']}")
        print(f"  {record['tool_calls']} tool calls, {record['seconds']}s")

    auto = [r for r in results if r["scoring"] == "execution"]
    passed = sum(1 for r in auto if r["passed"])
    behaviour = sum(1 for r in auto if r["behaviour_passed"])

    print(f"\n{'=' * 72}")
    print(f"execution match: {passed}/{len(auto)}")
    print(f"behaviour:       {behaviour}/{len(auto)}")
    print(f"rubric cases:    {len(results) - len(auto)} to review by hand")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwritten to {out}")
    return 0
