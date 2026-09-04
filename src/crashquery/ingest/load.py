"""Load STATS19 CSVs into Postgres.

Strategy: COPY into an all-TEXT staging table, then INSERT ... SELECT with
guarded casts into the typed table.

Loading straight into typed columns looks tidier and breaks constantly. STATS19
CSVs carry -1, empty strings and the occasional stray value in numeric columns,
and a COPY that hits one of those aborts the whole file after several minutes.
Staging plus regex-guarded casts never raises: a value that doesn't look like a
number becomes NULL and gets counted, so you find out how much you lost instead
of discovering it in a model six weeks later.
"""

from __future__ import annotations

import csv
import logging
from importlib.resources import files
from pathlib import Path

import psycopg
from psycopg import sql

from crashquery.ingest.roles import ensure_agent_role
from crashquery.ingest.sources import TABLE_COMMENTS, canonical_column, column_type, csv_filename
from crashquery.settings import get_settings

log = logging.getLogger(__name__)

# CSV table name -> Postgres table name
TARGET_TABLE = {
    "collision": "collisions",
    "vehicle": "vehicles",
    "casualty": "casualties",
}

INT_TYPES = {"SMALLINT", "INTEGER", "BIGINT"}
FLOAT_TYPES = {"DOUBLE PRECISION", "REAL", "NUMERIC"}

# Columns to index. Everything the agent is likely to filter or join on.
INDEXED_COLUMNS = {
    "collisions": [
        "accident_index",
        "accident_year",
        "accident_severity",
        "local_authority_ons_district",
        "collision_date",
    ],
    "vehicles": ["accident_index", "accident_year", "vehicle_type"],
    "casualties": [
        "accident_index",
        "accident_year",
        "casualty_severity",
        "casualty_class",
    ],
}


def _seed_lookups():
    return files("crashquery.ingest").joinpath("lookups_seed.csv")


# Current STATS19 files ship collision_* names. The agent, gold SQL and
# indexes still use the documented accident_* names.
COMPAT_COLUMNS = {
    "accident_index": "collision_index",
    "accident_year": "collision_year",
    "accident_reference": "collision_ref_no",
    "accident_severity": "collision_severity",
}


def read_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    return [canonical_column(h) for h in header if h.strip()]


def cast_expression(column: str) -> sql.Composed:
    """Guarded cast from staging TEXT to the target type.

    The regex test is what makes this total — no input can make it raise.
    """
    pg_type = column_type(column)
    col = sql.Identifier(column)

    if pg_type in INT_TYPES:
        return sql.SQL("CASE WHEN {c} ~ '^-?[0-9]+$' THEN {c}::{t} ELSE NULL END").format(
            c=col, t=sql.SQL(pg_type)
        )

    if pg_type in FLOAT_TYPES:
        return sql.SQL(
            "CASE WHEN {c} ~ '^-?[0-9]*\\.?[0-9]+([eE][-+]?[0-9]+)?$' "
            "THEN {c}::{t} ELSE NULL END"
        ).format(c=col, t=sql.SQL(pg_type))

    return sql.SQL("NULLIF(NULLIF({c}, ''), 'NULL')").format(c=col)


def ensure_table(conn: psycopg.Connection, table: str, columns: list[str]) -> None:
    """Create the table, or widen it if a later year adds columns."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        existing = {row[0] for row in cur.fetchall()}

        if not existing:
            definitions = sql.SQL(", ").join(
                sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(column_type(c)))
                for c in columns
            )
            cur.execute(
                sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(table), definitions)
            )
            log.info("created table %s with %d columns", table, len(columns))
            return

        # STATS19 gains and loses columns between releases. Add anything new so
        # a later year doesn't silently drop its extra fields.
        for column in columns:
            if column not in existing:
                cur.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                        sql.Identifier(table),
                        sql.Identifier(column),
                        sql.SQL(column_type(column)),
                    )
                )
                log.warning("added new column %s.%s", table, column)


def load_file(conn: psycopg.Connection, path: Path, table: str) -> int:
    columns = read_header(path)
    ensure_table(conn, table, columns)

    staging = f"stg_{table}"
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(staging)))
        cur.execute(
            sql.SQL("CREATE UNLOGGED TABLE {} ({})").format(
                sql.Identifier(staging),
                sql.SQL(", ").join(sql.SQL("{} TEXT").format(sql.Identifier(c)) for c in columns),
            )
        )

        copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
            sql.Identifier(staging),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        )

        log.info("copying %s", path.name)
        with cur.copy(copy_stmt) as copy, path.open("rb") as fh:
            while block := fh.read(1 << 20):
                copy.write(block)

        cur.execute(
            sql.SQL("INSERT INTO {tgt} ({cols}) SELECT {casts} FROM {stg}").format(
                tgt=sql.Identifier(table),
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                casts=sql.SQL(", ").join(cast_expression(c) for c in columns),
                stg=sql.Identifier(staging),
            )
        )
        inserted = cur.rowcount
        cur.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(staging)))

    conn.commit()
    log.info("  loaded %d rows into %s", inserted, table)
    return inserted


def add_derived_columns(conn: psycopg.Connection) -> None:
    """Parse the dd/mm/yyyy date once, at load time.

    Making every generated query re-parse a text date is both slow and a
    reliable source of agent errors, and date parsing isn't the skill this
    project is meant to demonstrate.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='collisions' "
            "AND column_name='date'"
        )
        if not cur.fetchone():
            return

        cur.execute("ALTER TABLE collisions ADD COLUMN IF NOT EXISTS collision_date DATE")
        cur.execute(
            "UPDATE collisions SET collision_date = "
            "  CASE WHEN date ~ '^\\d{2}/\\d{2}/\\d{4}$' "
            "       THEN to_date(date, 'DD/MM/YYYY') ELSE NULL END "
            "WHERE collision_date IS NULL"
        )
        log.info("populated collisions.collision_date (%d rows)", cur.rowcount)
    conn.commit()


def add_compat_columns(conn: psycopg.Connection) -> None:
    """Copy collision_* columns onto the accident_* names the agent expects.

    Recent STATS19 releases renamed these in the CSV. Existing databases
    loaded before the alias may only have collision_*.
    """
    with conn.cursor() as cur:
        for table in TARGET_TABLE.values():
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s",
                (table,),
            )
            present = {row[0] for row in cur.fetchall()}
            if not present:
                continue
            for canonical, current in COMPAT_COLUMNS.items():
                if current not in present:
                    continue
                pg_type = column_type(canonical)
                if canonical not in present:
                    cur.execute(
                        sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                            sql.Identifier(table),
                            sql.Identifier(canonical),
                            sql.SQL(pg_type),
                        )
                    )
                    present.add(canonical)
                src = sql.Identifier(current)
                if pg_type in INT_TYPES:
                    expr = sql.SQL(
                        "CASE WHEN {src}::text ~ '^-?[0-9]+$' THEN {src}::{t} ELSE NULL END"
                    ).format(src=src, t=sql.SQL(pg_type))
                else:
                    expr = sql.SQL("{src}::{t}").format(src=src, t=sql.SQL(pg_type))
                cur.execute(
                    sql.SQL("UPDATE {} SET {} = {} WHERE {} IS NULL").format(
                        sql.Identifier(table),
                        sql.Identifier(canonical),
                        expr,
                        sql.Identifier(canonical),
                    )
                )
                if cur.rowcount:
                    log.info("aliased %s.%s ← %s (%d rows)", table, canonical, current, cur.rowcount)
    conn.commit()


def load_lookups(conn: psycopg.Connection) -> int:
    """Load the code dictionary — the agent's most important tool.

    This is a curated subset covering the fields you'll actually query. The
    full official dictionary is the 'Road Safety Open Dataset Data Guide'
    spreadsheet on the gov.uk landing page; extend this CSV from it as needed.
    """
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS code_lookups")
        cur.execute(
            """
            CREATE TABLE code_lookups (
                table_name TEXT NOT NULL,
                field_name TEXT NOT NULL,
                code       INTEGER NOT NULL,
                label      TEXT NOT NULL,
                PRIMARY KEY (table_name, field_name, code)
            )
            """
        )
        with cur.copy(
            "COPY code_lookups (table_name, field_name, code, label) "
            "FROM STDIN WITH (FORMAT csv, HEADER true)"
        ) as copy:
            copy.write(_seed_lookups().read_bytes())

        cur.execute("SELECT count(*) FROM code_lookups")
        count = cur.fetchone()[0]

        cur.execute("CREATE INDEX ON code_lookups (field_name)")
    conn.commit()
    log.info("loaded %d code lookups", count)
    return count


def finalise(conn: psycopg.Connection) -> None:
    """Indexes, comments, constraints, statistics."""
    with conn.cursor() as cur:
        for table, columns in INDEXED_COLUMNS.items():
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s",
                (table,),
            )
            present = {row[0] for row in cur.fetchall()}
            if not present:
                continue
            for column in columns:
                if column not in present:
                    continue
                cur.execute(
                    sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                        sql.Identifier(f"ix_{table}_{column}"),
                        sql.Identifier(table),
                        sql.Identifier(column),
                    )
                )

        for table, comment in TABLE_COMMENTS.items():
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=%s",
                (table,),
            )
            if cur.fetchone():
                cur.execute(
                    sql.SQL("COMMENT ON TABLE {} IS {}").format(
                        sql.Identifier(table), sql.Literal(comment)
                    )
                )

        cur.execute("ANALYZE")
    conn.commit()
    log.info("indexes, comments and statistics done")

    # accident_index should be unique within collisions, but DfT has shipped
    # duplicates before. Try for the constraint; if it fails, say so loudly
    # rather than pretending the key is clean.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE collisions ADD CONSTRAINT collisions_pkey "
                "PRIMARY KEY (accident_index)"
            )
        conn.commit()
        log.info("primary key on collisions.accident_index added")
    except psycopg.errors.Error as exc:
        conn.rollback()
        log.warning(
            "could not add primary key on collisions.accident_index — %s. "
            "Duplicate accident_index values across years are a known STATS19 "
            "quirk; joins still work but are not guaranteed 1:N.",
            str(exc).splitlines()[0],
        )


def load_all(dsn: str, years: list[int], raw_dir: Path | None = None) -> None:
    raw_dir = raw_dir if raw_dir is not None else get_settings().raw_dir
    with psycopg.connect(dsn) as conn:
        total = 0
        for csv_table, pg_table in TARGET_TABLE.items():
            for year in years:
                path = raw_dir / csv_filename(csv_table, year)
                if not path.exists():
                    log.warning("missing %s — run crashquery download first", path.name)
                    continue
                total += load_file(conn, path, pg_table)

        add_derived_columns(conn)
        add_compat_columns(conn)
        load_lookups(conn)
        finalise(conn)
        ensure_agent_role(dsn)
        log.info("TOTAL %d rows loaded", total)


def run_load(
    *,
    dsn: str | None = None,
    years: list[int],
    raw_dir: Path | None = None,
    truncate: bool = False,
) -> None:
    settings = get_settings()
    dsn = dsn or settings.owner_dsn
    raw_dir = raw_dir if raw_dir is not None else settings.raw_dir

    if truncate:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            for table in list(TARGET_TABLE.values()) + ["code_lookups"]:
                cur.execute(
                    sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table))
                )
            conn.commit()
        log.info("dropped existing tables")

    load_all(dsn, years, raw_dir)


