"""Command line entry point.

    crashquery check
    crashquery ask "How many people were killed on the roads in 2022?"
    crashquery chat
    crashquery download --from-year 2019 --to-year 2023
    crashquery load --from-year 2019 --to-year 2023
    crashquery eval
    crashquery eval --only fatal_casualties_2022
    crashquery tui
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from crashquery.settings import get_settings


def cmd_check() -> int:
    from crashquery.agent.db import list_tables
    from crashquery.ingest.roles import ensure_agent_role

    try:
        tables = list_tables()
    except Exception as first:
        try:
            role = ensure_agent_role()
        except Exception as repair:
            print(f"Cannot reach the database: {first}", file=sys.stderr)
            print(
                "\nCould not create the read-only role "
                f"({repair}). Is Postgres running?  docker compose up -d",
                file=sys.stderr,
            )
            return 1
        try:
            tables = list_tables()
            print(f"Created missing role {role} and connected.\n")
        except Exception as exc:
            print(f"Cannot reach the database: {exc}", file=sys.stderr)
            print("\nIs it running?  docker compose up -d", file=sys.stderr)
            return 1

    if not tables:
        print("Connected, but no tables. Run the ingestion:", file=sys.stderr)
        print("  crashquery download && crashquery load", file=sys.stderr)
        return 1

    print("Database OK.\n")
    for row in tables:
        count = row["approx_rows"]
        print(
            f"  {row['table_name']:<16} ~{count:>12,} rows"
            if count and count > 0
            else f"  {row['table_name']:<16} {'unknown':>13}"
        )
    return 0


def render(outcome: dict) -> None:
    print(f"\n{outcome['answer']}\n")
    if outcome["trace"]:
        tools = ", ".join(t["tool"] for t in outcome["trace"])
        print(f"[{outcome['tool_calls']} tool calls: {tools}]")
    if not outcome["consulted_codes"] and outcome["sql_executed"]:
        print("[warning: ran SQL without consulting the code dictionary]")


def cmd_ask(question: str, model: str | None) -> int:
    from crashquery.agent.graph import ask, build_agent

    try:
        agent = build_agent(model) if model else build_agent()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    render(ask(agent, question))
    return 0


def cmd_chat(model: str | None) -> int:
    from langgraph.checkpoint.memory import InMemorySaver

    from crashquery.agent.graph import ask, build_agent

    try:
        agent = (
            build_agent(model, checkpointer=InMemorySaver())
            if model
            else build_agent(checkpointer=InMemorySaver())
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print("Road safety analyst. Ctrl-C or 'exit' to quit.\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in {"exit", "quit"}:
            return 0
        if not question:
            continue
        try:
            render(ask(agent, question, thread_id="chat"))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)


def cmd_download(from_year: int, to_year: int) -> int:
    from crashquery.ingest.download import download_years

    settings = get_settings()
    years = list(range(from_year, to_year + 1))
    paths = download_years(years, dest_dir=settings.raw_dir)
    print(f"\n{len(paths)} files in {settings.raw_dir}")
    return 0


def cmd_load(from_year: int, to_year: int, dsn: str | None, truncate: bool) -> int:
    from crashquery.ingest.load import run_load

    years = list(range(from_year, to_year + 1))
    run_load(dsn=dsn, years=years, truncate=truncate)
    return 0


def cmd_tui(model: str | None) -> int:
    from crashquery.tui.app import run_tui

    return run_tui(model)


def cmd_eval(model: str | None, only: str | None, out: Path) -> int:
    from crashquery.evaluation.harness import run_eval

    try:
        return run_eval(model=model, only=only, out=out)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Text-to-SQL agent over the UK STATS19 road casualty database.",
        epilog=(
            'examples:\n'
            '  crashquery check\n'
            '  crashquery ask "How many people were killed on the roads in 2022?"\n'
            '  crashquery chat\n'
            '  crashquery download --from-year 2019 --to-year 2023\n'
            '  crashquery load --from-year 2019 --to-year 2023\n'
            '  crashquery eval --only fatal_casualties_2022\n'
            '  crashquery tui\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model", help="e.g. anthropic:claude-sonnet-4-5")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    ask_parser = sub.add_parser("ask", help="ask one question")
    ask_parser.add_argument("question")
    sub.add_parser("chat", help="interactive session")
    sub.add_parser("check", help="verify database connectivity and contents")

    download_parser = sub.add_parser("download", help="fetch STATS19 CSVs")
    download_parser.add_argument("--from-year", type=int, default=2019)
    download_parser.add_argument("--to-year", type=int, default=2023)

    load_parser = sub.add_parser("load", help="load CSVs into Postgres")
    load_parser.add_argument("--from-year", type=int, default=2019)
    load_parser.add_argument("--to-year", type=int, default=2023)
    load_parser.add_argument(
        "--dsn",
        default=None,
        help="owner DSN (NOT the agent's read-only role); defaults to OWNER_DSN",
    )
    load_parser.add_argument("--truncate", action="store_true", help="drop tables before loading")

    eval_parser = sub.add_parser("eval", help="run the gold evaluation set")
    eval_parser.add_argument("--only", help="run a single case by id")
    eval_parser.add_argument("--out", type=Path, default=Path("eval/results.json"))
    sub.add_parser("tui", help="interactive Textual interface")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()

    if args.verbose:
        level = logging.DEBUG
    elif args.command in {"download", "load", "eval"}:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    if args.command == "check":
        code = cmd_check()
    elif args.command == "ask":
        code = cmd_ask(args.question, args.model)
    elif args.command == "chat":
        code = cmd_chat(args.model)
    elif args.command == "download":
        code = cmd_download(args.from_year, args.to_year)
    elif args.command == "load":
        code = cmd_load(args.from_year, args.to_year, args.dsn, args.truncate)
    elif args.command == "tui":
        code = cmd_tui(args.model)
    else:
        code = cmd_eval(args.model, args.only, args.out)

    raise SystemExit(code)
