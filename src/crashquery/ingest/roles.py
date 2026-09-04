"""Ensure the read-only agent role exists.

Postgres only runs docker-entrypoint-initdb.d on first boot of an empty
volume. If the volume already existed, rsa_agent is missing and
`crashquery check` fails with a password error. Recreating the role here
is idempotent and is the path that actually works after a restart.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from crashquery.settings import get_settings

log = logging.getLogger(__name__)


def ensure_agent_role(owner_dsn: str | None = None) -> str:
    """Create or repair the read-only agent login. Returns the role name."""
    settings = get_settings()
    owner_dsn = owner_dsn or settings.owner_dsn
    info = conninfo_to_dict(settings.agent_dsn)
    role = info.get("user") or "rsa_agent"
    password = info.get("password") or "rsa_agent_pw"
    role_id = sql.Identifier(role)

    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        dbname = conn.info.dbname
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        role_id, sql.Literal(password)
                    )
                )
                log.info("reset password for role %s", role)
            else:
                cur.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                        role_id, sql.Literal(password)
                    )
                )
                log.info("created role %s", role)

            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(dbname), role_id
                )
            )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role_id))
            cur.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role_id)
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
                ).format(role_id)
            )
            cur.execute(
                sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(
                    sql.Identifier(dbname), role_id
                )
            )
            cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '30s'").format(role_id))
            cur.execute(
                sql.SQL(
                    "ALTER ROLE {} SET idle_in_transaction_session_timeout = '10s'"
                ).format(role_id)
            )
            cur.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role_id)
            )

    return role
