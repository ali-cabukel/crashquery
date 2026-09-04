"""Textual TUI for asking STATS19 questions and inspecting the generated SQL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.syntax import Syntax
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.suggester import Suggester
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

from crashquery.settings import get_settings
from crashquery.tui.formatters import describe_markdown, last_sql, table_rows, tools_markup

HELP = """\
# crashquery

Ask a question in plain English. The agent looks up STATS19 codes, writes
PostgreSQL, and answers with the SQL it used.

## Commands
- `/tables` — refresh the schema pane
- `/describe <table>` — columns and coded fields
- `/sql` — jump the side pane to the last query
- `/check` — database connectivity
- `/clear` — clear the conversation
- `/help` — this page
- `/quit` — leave

## Try
- How many people were killed on the roads in 2022?
- How many collisions in 2022 involved at least one fatality?
- What is the average age of casualties in 2022?
- Break down pedestrian casualties in 2022 by severity.
"""

SLASH_COMMANDS = (
    "/help",
    "/tables",
    "/describe collisions",
    "/describe casualties",
    "/describe vehicles",
    "/sql",
    "/check",
    "/clear",
    "/quit",
)


class SlashSuggester(Suggester):
    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None
        hits = [cmd for cmd in SLASH_COMMANDS if cmd.startswith(value)]
        return hits[0] if hits else None


class CrashqueryApp(App[None]):
    CSS_PATH = Path(__file__).with_name("app.tcss")
    TITLE = "crashquery"
    BINDINGS = [
        Binding("ctrl+c", "quit", "quit", show=True),
        Binding("escape", "quit", "quit", show=False),
        Binding("ctrl+l", "clear", "clear", show=True),
        Binding("f1", "help", "help", show=True),
    ]

    def __init__(self, model: str | None = None) -> None:
        super().__init__()
        settings = get_settings()
        self._model = model or settings.model
        self._agent: Any = None
        self._busy = False
        self._last_sql = ""
        self.sub_title = self._model

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="chat-pane"):
                yield RichLog(id="chat", highlight=True, markup=True, wrap=True)
            with Vertical(id="side"):
                yield Label("tables", id="schema-title")
                yield DataTable(id="schema", cursor_type="row")
                yield Label("last sql", id="sql-title")
                yield RichLog(id="sql", highlight=True, markup=True, wrap=True)
                yield Label("tools", id="tools-title")
                yield Static("[dim]no tools yet[/]", id="tools")
        yield Label("type a question  ·  /tables  ·  /help", id="status")
        yield Input(
            placeholder="How many people were killed on the roads in 2022?  ·  /help",
            id="command",
            suggester=SlashSuggester(),
        )
        yield Footer()

    def on_mount(self) -> None:
        schema = self.query_one("#schema", DataTable)
        schema.add_columns("table", "rows")
        self._write_chat(Markdown(HELP))
        self.query_one("#command", Input).focus()
        self._job_refresh_schema()

    def action_help(self) -> None:
        self._cmd_help()

    def action_clear(self) -> None:
        self._cmd_clear()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._run_command(text)
            return
        self._ask(text)

    def _write_chat(self, renderable: object) -> None:
        self.query_one("#chat", RichLog).write(renderable)

    def _ok(self, message: str) -> None:
        status = self.query_one("#status", Label)
        status.update(message)
        status.remove_class("-error")
        status.remove_class("-busy")

    def _busy_status(self, message: str) -> None:
        status = self.query_one("#status", Label)
        status.update(message)
        status.remove_class("-error")
        status.add_class("-busy")

    def _error(self, message: str) -> None:
        self._busy = False
        self.query_one("#command", Input).disabled = False
        status = self.query_one("#status", Label)
        status.update(message)
        status.remove_class("-busy")
        status.add_class("-error")
        self._write_chat(f"[red]{message}[/]")

    def _run_command(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        name = name.lower()
        handler = {
            "help": lambda _arg: self._cmd_help(),
            "h": lambda _arg: self._cmd_help(),
            "tables": lambda _arg: self._cmd_tables(),
            "check": lambda _arg: self._cmd_check(),
            "describe": self._cmd_describe,
            "sql": lambda _arg: self._cmd_sql(),
            "clear": lambda _arg: self._cmd_clear(),
            "quit": lambda _arg: self.exit(),
            "q": lambda _arg: self.exit(),
        }.get(name)
        if handler is None:
            self._error(f"unknown command: /{name}  (try /help)")
            return
        try:
            handler(arg.strip())
        except Exception as exc:
            self._error(str(exc))

    def _cmd_help(self) -> None:
        self._write_chat(Markdown(HELP))
        self._ok("commands")

    def _cmd_clear(self) -> None:
        self.query_one("#chat", RichLog).clear()
        self._write_chat(Markdown(HELP))
        self._ok("cleared")

    def _cmd_sql(self) -> None:
        if not self._last_sql:
            self._ok("no SQL yet")
            return
        self._show_sql(self._last_sql)
        self._ok("last SQL")

    def _cmd_tables(self) -> None:
        self._job_refresh_schema()

    def _cmd_check(self) -> None:
        self._job_refresh_schema()

    def _cmd_describe(self, table: str) -> None:
        if not table:
            raise ValueError("usage: /describe <table>")
        self._job_describe(table)

    def _fill_schema(self, rows: list[tuple[str, str]], status: str) -> None:
        table = self.query_one("#schema", DataTable)
        table.clear()
        for name, estimate in rows:
            table.add_row(name, estimate)
        self._ok(status)

    def _show_sql(self, query: str) -> None:
        pane = self.query_one("#sql", RichLog)
        pane.clear()
        pane.write(Syntax(query, "sql", theme="github-dark", word_wrap=True))

    def _show_outcome(self, question: str, outcome: dict[str, Any]) -> None:
        self._busy = False
        self.query_one("#command", Input).disabled = False
        self._write_chat(f"[bold cyan]you[/]  {question}")
        answer = outcome.get("answer") or "(no answer)"
        self._write_chat(Markdown(answer))
        sql = last_sql(outcome.get("sql_executed") or [])
        self._last_sql = sql
        if sql:
            self._show_sql(sql)
        self.query_one("#tools", Static).update(tools_markup(outcome.get("trace") or []))
        n = outcome.get("tool_calls") or 0
        note = f"{n} tool call{'s' if n != 1 else ''}"
        if sql and not outcome.get("consulted_codes"):
            note += "  ·  ran SQL without lookup_codes"
        self._ok(note)

    def _ask(self, question: str) -> None:
        if self._busy:
            self._error("already running a question")
            return
        self._busy = True
        self.query_one("#command", Input).disabled = True
        self._busy_status("asking the agent…")
        self._job_ask(question)

    @work(thread=True, exclusive=True)
    def _job_ask(self, question: str) -> None:
        try:
            if self._agent is None:
                from langgraph.checkpoint.memory import InMemorySaver

                from crashquery.agent.graph import build_agent

                self._agent = build_agent(self._model, checkpointer=InMemorySaver())
            from crashquery.agent.graph import ask

            outcome = ask(self._agent, question, thread_id="tui")
        except Exception as exc:
            self.call_from_thread(self._error, str(exc))
            return
        self.call_from_thread(self._show_outcome, question, outcome)

    @work(thread=True, exclusive=True)
    def _job_refresh_schema(self) -> None:
        try:
            from crashquery.agent.db import list_tables
            from crashquery.ingest.roles import ensure_agent_role

            try:
                tables = list_tables()
            except Exception:
                ensure_agent_role()
                tables = list_tables()
        except Exception as exc:
            self.call_from_thread(self._error, f"database: {exc}")
            return
        if not tables:
            self.call_from_thread(
                self._error, "no tables — run crashquery download && crashquery load"
            )
            return
        rows = table_rows(tables)
        self.call_from_thread(self._fill_schema, rows, f"{len(rows)} tables")

    @work(thread=True, exclusive=True)
    def _job_describe(self, table: str) -> None:
        try:
            from crashquery.agent.db import describe_table

            info = describe_table(table)
        except Exception as exc:
            self.call_from_thread(self._error, str(exc))
            return
        if not info.get("columns"):
            self.call_from_thread(self._error, f"no table named {table!r}")
            return
        self.call_from_thread(self._write_chat, Markdown(describe_markdown(info)))
        self.call_from_thread(self._ok, f"described {table}")


def run_tui(model: str | None = None) -> int:
    CrashqueryApp(model=model).run()
    return 0
