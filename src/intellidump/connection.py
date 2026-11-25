r"""Opening the source connection.

Anything not given explicitly falls back to libpq's own defaults, exactly as
with pg_dump: ``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``,
``PGDATABASE``, ``PGSERVICE``, ``PGSSLMODE``, the ``~/.pgpass`` file
(``%APPDATA%\postgresql\pgpass.conf`` on Windows), and so on.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

log = logging.getLogger(__name__)

APPLICATION_NAME = "intellidump"
#: Seconds; used unless PGCONNECT_TIMEOUT or the conninfo sets connect_timeout.
DEFAULT_CONNECT_TIMEOUT = 30
#: TCP keepalives, used unless the conninfo sets them: probe a quiet connection
#: after a minute, so firewalls and NAT gateways don't drop the connection that
#: keeps the snapshot open, and a dead server is noticed within about two minutes.
DEFAULT_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 10,
    "keepalives_count": 6,
}

#: A zero-argument function that opens a new connection to the source database.
Connector = Callable[[], "psycopg.Connection[Any]"]

#: Settings for the dump's transaction. The first group makes ``value::text``
#: output unambiguous and re-parseable whatever the server or role defaults are.
#: row_security = off makes a query fail rather than silently skip rows hidden by
#: row-level security (as pg_dump does). The timeouts are disabled, as pg_dump
#: does, because a dump runs long and sits idle while rows are written to the
#: work database (transaction_timeout, on PostgreSQL 17+, is added by
#: :func:`dump_settings`).
SESSION_SETTINGS = {
    "DateStyle": "ISO",
    "IntervalStyle": "postgres",
    "extra_float_digits": "3",
    "bytea_output": "hex",
    "client_encoding": "UTF8",
    "standard_conforming_strings": "on",
    "row_security": "off",
    "idle_in_transaction_session_timeout": "0",
    "statement_timeout": "0",
    "lock_timeout": "0",
}

#: Settings never copied from the caller's session to a new connection.
_NOT_COPIED = {"transaction_isolation", "transaction_read_only", "transaction_deferrable"}


@dataclass
class ConnectionOptions:
    """How to reach the source database; unset fields fall back to libpq defaults.

    Args:
        host: Server host or socket directory.
        port: Server port.
        username: Database user.
        password: Password; prefer PGPASSWORD or a password file.
        dbname: Database name, or a conninfo string / ``postgresql://`` URI.
        prompt_password: Prompt for the password before connecting (pg_dump ``-W``).
        no_password: Never prompt for a password (pg_dump ``-w``).
    """

    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    dbname: str | None = None
    prompt_password: bool = False
    no_password: bool = False


def _looks_like_conninfo(value: str) -> bool:
    """Tell a conninfo string or URI from a plain database name.

    Args:
        value: The ``dbname`` option.

    Returns:
        True if it should be passed to libpq as a conninfo.
    """
    return "=" in value or value.startswith(("postgresql://", "postgres://"))


def build_connect_args(opts: ConnectionOptions) -> tuple[str, dict[str, Any]]:
    """Build the arguments for psycopg.connect.

    Args:
        opts: Connection options; explicit ones override a conninfo ``dbname``.

    Returns:
        ``(conninfo, kwargs)`` for ``psycopg.connect``.
    """
    conninfo = ""
    given: dict[str, Any] = {}
    kwargs: dict[str, Any] = {}
    if opts.dbname:
        if _looks_like_conninfo(opts.dbname):
            given = conninfo_to_dict(opts.dbname)  # also validates, with a clear error
            conninfo = opts.dbname
        else:
            kwargs["dbname"] = opts.dbname
    if opts.host:
        kwargs["host"] = opts.host
    if opts.port is not None:
        kwargs["port"] = opts.port
    if opts.username:
        kwargs["user"] = opts.username
    if opts.password is not None:
        kwargs["password"] = opts.password
    if "application_name" not in given and "PGAPPNAME" not in os.environ:
        kwargs["application_name"] = APPLICATION_NAME
    # libpq waits forever by default; an unreachable host should fail eventually.
    if "connect_timeout" not in given and "PGCONNECT_TIMEOUT" not in os.environ:
        kwargs["connect_timeout"] = DEFAULT_CONNECT_TIMEOUT
    for name, value in DEFAULT_KEEPALIVES.items():
        if name not in given:
            kwargs[name] = value
    return conninfo, kwargs


def _password_was_missing(exc: psycopg.OperationalError) -> bool:
    """Tell whether a connection failed for want of a password.

    Args:
        exc: The connection error.

    Returns:
        True if prompting for a password could help.
    """
    text = str(exc).lower()
    return "no password supplied" in text or "password authentication failed" in text


def connect(opts: ConnectionOptions) -> psycopg.Connection[Any]:
    """Connect, prompting for a password like pg_dump if the server wants one.

    Args:
        opts: Connection options; ``password`` is filled in when prompted.

    Returns:
        An open connection with the session settings the dump relies on.
    """
    if opts.prompt_password and opts.password is None:
        opts.password = getpass.getpass("Password: ")

    conninfo, kwargs = build_connect_args(opts)
    try:
        conn = psycopg.connect(conninfo, **kwargs)
    except psycopg.OperationalError as exc:
        can_prompt = (
            opts.password is None
            and not opts.no_password
            and sys.stdin is not None
            and sys.stdin.isatty()
        )
        if not (can_prompt and _password_was_missing(exc)):
            raise
        opts.password = getpass.getpass("Password: ")
        conninfo, kwargs = build_connect_args(opts)
        conn = psycopg.connect(conninfo, **kwargs)

    log.info(
        "connected to %s:%s database %s as %s (server %s)",
        conn.info.host,
        conn.info.port,
        conn.info.dbname,
        conn.info.user,
        conn.info.server_version,
    )
    return conn


def reconnector(conn: psycopg.Connection[Any]) -> Connector:
    """Make a function that opens new connections like an existing one.

    Args:
        conn: Open connection whose parameters (and password) are reused.

    Returns:
        A connector; keepalives are added unless the connection sets them.
    """
    dsn = conn.info.dsn
    password = conn.info.password
    given = conninfo_to_dict(dsn)
    kwargs: dict[str, Any] = {k: v for k, v in DEFAULT_KEEPALIVES.items() if k not in given}
    if password:
        kwargs["password"] = password
    return lambda: psycopg.connect(dsn, **kwargs)


def copied_settings(conn: psycopg.Connection[Any]) -> list[tuple[str, str]]:
    """Read the settings a session has changed with SET.

    New connections opened to replace a lost one get these too, so the query
    behaves the same on them (``TimeZone``, ``search_path``, ``lc_monetary``, ...)
    and runs with the same privileges (``SET ROLE`` and ``SET SESSION
    AUTHORIZATION``, which pg_settings doesn't list). Custom settings such as
    ``myapp.tenant`` can't be listed by PostgreSQL, so they aren't copied.

    Args:
        conn: The caller's connection, outside any transaction.

    Returns:
        ``(name, value)`` pairs in the order they must be applied.
    """
    rows = conn.execute(
        "SELECT 0, 'session_authorization', pg_catalog.current_setting('session_authorization') "
        "WHERE pg_catalog.current_setting('session_authorization') <> %(login)s "
        "UNION ALL SELECT 1, name, setting FROM pg_catalog.pg_settings WHERE source = 'session' "
        "UNION ALL SELECT 2, 'role', pg_catalog.current_setting('role') "
        "WHERE pg_catalog.current_setting('role') <> 'none' "
        "ORDER BY 1, 2",
        {"login": conn.info.user},
    ).fetchall()
    if not conn.autocommit:
        conn.rollback()
    return [(name, value) for _, name, value in rows if name not in _NOT_COPIED]


def dump_settings(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """The settings for the dump's transaction on this server.

    Args:
        conn: Open connection.

    Returns:
        SESSION_SETTINGS, plus the settings only newer servers have.
    """
    settings = dict(SESSION_SETTINGS)
    if conn.info.server_version >= 170000:
        settings["transaction_timeout"] = "0"
    return settings


def begin_snapshot(
    conn: psycopg.Connection[Any],
    *,
    snapshot: str | None = None,
    settings: Sequence[tuple[str, str]] = (),
) -> None:
    """Start the dump's read-only REPEATABLE READ transaction.

    Applies the settings for the length of the transaction only, so the
    connection's own settings come back when it ends.

    Args:
        conn: Connection to use; any open transaction is rolled back.
        snapshot: Snapshot exported by another transaction (``pg_export_snapshot``)
            to share, so both see exactly the same data; None for a new one.
        settings: ``(name, value)`` pairs applied first; dump_settings() win.
    """
    conn.rollback()
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    conn.autocommit = False
    if snapshot is not None:
        # Must be the transaction's first statement. Utility statements take no
        # parameters, so the identifier is quoted as a literal instead.
        conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
    for name, value in [*settings, *dump_settings(conn).items()]:
        conn.execute("SELECT pg_catalog.set_config(%s, %s, true)", (name, value))
