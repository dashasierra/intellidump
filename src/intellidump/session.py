"""Keeping the dump's snapshot across lost connections.

Everything is read inside one REPEATABLE READ snapshot, so the export is
consistent even while the source keeps changing. Simply reconnecting would give
a new snapshot, and mixing rows read at two points in time can produce a file
that doesn't import (say, a child read before its parent was deleted). So:

* The *holder*, a second connection, opens the snapshot, exports it with
  ``pg_export_snapshot()``, and then sits idle, keeping it alive.
* The *worker* (the caller's own connection, to begin with) imports it with
  ``SET TRANSACTION SNAPSHOT`` and runs every query. If the worker's connection
  is lost, a new worker imports the same snapshot, with the session settings
  the caller's connection had, and the interrupted query runs again, seeing
  exactly the same rows.
* If the snapshot itself is gone (the holder's connection was lost too, as when
  the server restarts), :class:`SnapshotLost` tells the dump to start over.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import time
from collections.abc import Sequence
from typing import Any

import psycopg

from .connection import Connector, begin_snapshot
from .errors import IntellidumpError

log = logging.getLogger(__name__)

#: SQLSTATEs meaning the session or its transaction was ended from outside, as
#: opposed to an error in the query itself. Class 08 (connection exception) too.
_LOST_STATES = {
    "57P01",  # admin_shutdown: server shutdown or pg_terminate_backend()
    "57P02",  # crash_shutdown: another backend crashed
    "57P03",  # cannot_connect_now: starting up or shutting down
    "57P05",  # idle_session_timeout
    "25P03",  # idle_in_transaction_session_timeout
    "25P04",  # transaction_timeout
    "40001",  # on a standby: canceled by a conflict with recovery
}
#: Connection failures that waiting won't fix: authentication, unknown database
#: or role. libpq gives no SQLSTATE when connecting fails, so the message is used.
_PERMANENT_PREFIXES = ("28", "3D")
_PERMANENT_MESSAGES = (
    "authentication failed",
    "no password supplied",
    "does not exist",
    "pg_hba.conf",
    "permission denied",
)
#: Tells one server instance and database from another: two connections with the
#: same value share a postmaster, so a snapshot exported by one is the other's.
_SERVER_IDENTITY = (
    "pg_catalog.pg_postmaster_start_time()::text || ' ' || pg_catalog.current_database()"
)
#: Seconds between reconnection attempts: doubling, up to the last value.
_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)


class SnapshotLost(IntellidumpError):
    """The connection was lost and the dump's snapshot can't be recovered."""


def connection_lost(exc: BaseException, conn: psycopg.Connection[Any] | None = None) -> bool:
    """Tell a lost connection from an error in the query.

    Args:
        exc: The error.
        conn: The connection the error came from, if known.

    Returns:
        True if the session ended (network failure, server restart, the backend
        being terminated, a timeout ending the session).
    """
    if not isinstance(exc, psycopg.Error):
        return False
    if conn is not None and conn.broken:
        return True
    state = exc.sqlstate or ""
    return state.startswith("08") or state in _LOST_STATES


def _brief(exc: BaseException) -> str:
    """First line of an error message.

    Args:
        exc: The error.

    Returns:
        Text for a one-line log message.
    """
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def describe_loss(exc: BaseException) -> str:
    """Describe why a dump attempt failed, for the log.

    Args:
        exc: The error.

    Returns:
        ``lost the connection to the server: <reason>``, or the SnapshotLost message.
    """
    if isinstance(exc, SnapshotLost):
        return str(exc)
    return f"lost the connection to the server: {_brief(exc)}"


def _permanent(exc: psycopg.OperationalError) -> bool:
    """Tell whether a failure to connect will happen again however long we wait.

    Args:
        exc: The connection error.

    Returns:
        True for authentication failures, unknown databases or roles, and the like.
    """
    if (exc.sqlstate or "").startswith(_PERMANENT_PREFIXES):
        return True
    text = str(exc).lower()
    return any(m in text for m in _PERMANENT_MESSAGES)


def _usable(conn: psycopg.Connection[Any] | None) -> bool:
    """Tell whether a connection is still open.

    Args:
        conn: Connection, or None.

    Returns:
        True if it can run queries.
    """
    return conn is not None and not conn.closed and not conn.broken


class Session:
    """The dump's connections, which survive lost connections where they can.

    With ``retries`` 0 there is a single connection, and any lost connection
    fails the dump.
    """

    def __init__(
        self,
        connect: Connector,
        *,
        retries: int = 5,
        reconnect_timeout: float = 300.0,
        first: psycopg.Connection[Any] | None = None,
        settings: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Set up the session; nothing is opened until :meth:`start`.

        Args:
            connect: Opens a new connection to the source database.
            retries: Times to reconnect and re-run any one query; 0 for a single
                connection and no reconnecting.
            reconnect_timeout: Seconds to keep trying to reconnect after a loss.
            first: Already open connection to use first; it is never closed here.
            settings: ``(name, value)`` session settings for every connection.
        """
        if retries < 0 or reconnect_timeout < 0:
            raise ValueError("retries and reconnect_timeout must not be negative")
        self._connect = connect
        self.retries = retries
        self.reconnect_timeout = reconnect_timeout
        self._first = first
        self._settings = list(settings)
        self._owned: list[psycopg.Connection[Any]] = []
        self._search_path: str | None = None
        self.holder: psycopg.Connection[Any] | None = None
        self.snapshot: str | None = None
        self._server: str | None = None
        self.conn: psycopg.Connection[Any] = first  # type: ignore[assignment]
        self.origin: tuple[str | None, int | None, str | None] = (None, None, None)
        self.reconnects = 0

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        """Open the connections and begin the snapshot.

        The first connection (the caller's, when it still works) does the work,
        so a dump that loses no connection runs exactly in the caller's session.
        If it doesn't work, a new one is opened, waiting up to
        ``reconnect_timeout`` for the server.
        """
        if _usable(self._first):
            assert self._first is not None
            first, settings = self._first, []
        else:
            first, settings = self._open(wait=self.retries > 0), self._settings
        self.origin = (first.info.host, first.info.port, first.info.dbname)
        self.conn = first
        if self.retries == 0:
            begin_snapshot(first, settings=settings)
            return
        holder: psycopg.Connection[Any] | None = None
        try:
            holder = self._open(wait=False)
            begin_snapshot(holder)
            row = holder.execute(
                f"SELECT pg_catalog.pg_export_snapshot(), {_SERVER_IDENTITY}"
            ).fetchone()
            assert row is not None
            self.holder, self.snapshot, self._server = holder, str(row[0]), str(row[1])
            self._join(first, settings)
        except (psycopg.Error, SnapshotLost) as exc:
            if not _usable(first):
                raise
            # E.g. no free connection slot, or a proxy or multi-host conninfo that
            # sent the second connection to another server.
            self._discard(holder)
            self.holder = self.snapshot = self._server = None
            log.warning(
                "could not keep the snapshot on a second connection (%s); a lost "
                "connection will start the dump over",
                _brief(exc),
            )
            begin_snapshot(first, settings=settings)
            return
        log.debug("snapshot %s held on a second connection", self.snapshot)

    def close(self) -> None:
        """End the snapshot and close the connections this session opened."""
        for conn in {id(c): c for c in (self.conn, self.holder, self._first) if c}.values():
            if _usable(conn):
                with contextlib.suppress(psycopg.Error):
                    conn.rollback()
        for conn in self._owned:
            conn.close()
        self._owned.clear()

    def __enter__(self) -> Session:
        """Start the session.

        Returns:
            The session.
        """
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the session.

        Args:
            exc: Exception details, if any.
        """
        self.close()

    # -- session state ----------------------------------------------------------

    def set_search_path(self, schemas: Sequence[str]) -> None:
        """Put schemas first on the search_path, here and on any later worker.

        Args:
            schemas: Schemas to prepend; the search_path is unchanged if empty.
        """
        cur = self.conn.cursor()
        if schemas:
            cur.execute(
                "SELECT pg_catalog.set_config('search_path', "
                "(SELECT string_agg(quote_ident(s), ', ') FROM unnest(%s::text[]) s) "
                "|| ', ' || current_setting('search_path'), true)",
                (list(schemas),),
            )
        row = cur.execute("SELECT pg_catalog.current_setting('search_path')").fetchone()
        assert row is not None
        self._search_path = str(row[0])

    # -- recovery ---------------------------------------------------------------

    def recover(self, exc: BaseException, attempt: int) -> bool:
        """Replace a lost worker with one in the same snapshot.

        Args:
            exc: The error a query failed with.
            attempt: How many times this query was re-run already.

        Returns:
            True if the query can be run again; False if ``exc`` is not a lost
            connection.

        Raises:
            SnapshotLost: The connection was lost and the snapshot with it.
            IntellidumpError: The query lost its connection too many times, or the
                server did not come back within ``reconnect_timeout``.
        """
        if not connection_lost(exc, self.conn):
            return False
        if self.holder is None:
            raise SnapshotLost(f"lost the connection to the server: {_brief(exc)}") from exc
        if attempt >= self.retries:
            raise IntellidumpError(
                f"lost the connection {attempt + 1} times running the same query, giving up: "
                f"{_brief(exc)}"
            ) from exc
        self._discard(self.conn)
        if not self._holder_alive():
            self.holder = None
            raise SnapshotLost(
                f"lost the connection to the server, and the snapshot with it: {_brief(exc)}"
            ) from exc
        log.warning(
            "lost the connection to the server (%s); reconnecting to run the query again "
            "in the same snapshot (retry %d of %d)",
            _brief(exc),
            attempt + 1,
            self.retries,
        )
        worker = self._open(wait=True)
        try:
            self._join(worker, self._settings)
        except SnapshotLost:
            self._discard(worker)
            raise
        except psycopg.Error as err:
            self._discard(worker)
            # 22023: the holder's transaction ended, so its snapshot is unknown.
            if connection_lost(err, worker) or err.sqlstate == "22023":
                raise SnapshotLost(f"could not rejoin the snapshot: {_brief(err)}") from err
            raise
        self.conn = worker
        self.reconnects += 1
        return True

    def can_restart(self, exc: BaseException) -> bool:
        """Tell whether a failed dump attempt should start over.

        Args:
            exc: The error the attempt failed with.

        Returns:
            True if the snapshot or the connection was lost and retries are allowed.
        """
        if self.retries == 0:
            return False
        return isinstance(exc, SnapshotLost) or connection_lost(exc, self.conn)

    # -- internals --------------------------------------------------------------

    def _open(self, *, wait: bool) -> psycopg.Connection[Any]:
        """Open a connection, retrying while the server is unreachable.

        Args:
            wait: Keep trying for up to ``reconnect_timeout`` seconds.

        Returns:
            The new connection, owned by this session.
        """
        deadline = time.monotonic() + self.reconnect_timeout
        for attempt in itertools.count():
            try:
                conn = self._connect()
            except psycopg.OperationalError as exc:
                if not wait or _permanent(exc):
                    raise
                delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                if time.monotonic() + delay > deadline:
                    raise IntellidumpError(
                        f"could not reconnect within {self.reconnect_timeout:g} s: {_brief(exc)}"
                    ) from exc
                log.info("cannot connect yet (%s); trying again in %g s", _brief(exc), delay)
                time.sleep(delay)
                continue
            self._owned.append(conn)
            return conn
        raise AssertionError("unreachable")  # pragma: no cover

    def _join(self, conn: psycopg.Connection[Any], settings: Sequence[tuple[str, str]]) -> None:
        """Begin a worker transaction in the held snapshot.

        Args:
            conn: The worker connection.
            settings: Session settings to give it first.
        """
        settings = list(settings)
        if self._search_path is not None:
            settings.append(("search_path", self._search_path))
        begin_snapshot(conn, snapshot=self.snapshot, settings=settings)
        # A snapshot identifier is only unique within one server's lifetime.
        row = conn.execute(f"SELECT {_SERVER_IDENTITY}").fetchone()
        if row is None or str(row[0]) != self._server:
            raise SnapshotLost("the new connection reached a different server")

    def _holder_alive(self) -> bool:
        """Check that the snapshot's holder connection still works.

        Returns:
            True if it answered.
        """
        if not _usable(self.holder):
            return False
        assert self.holder is not None
        try:
            self.holder.execute("SELECT 1")
        except psycopg.Error:
            return False
        return True

    def _discard(self, conn: psycopg.Connection[Any] | None) -> None:
        """Close a connection this session opened, ignoring errors.

        Args:
            conn: Connection to drop; the caller's own connection is left open.
        """
        if conn is None or conn is self._first:
            return
        if conn in self._owned:
            self._owned.remove(conn)
        with contextlib.suppress(psycopg.Error):
            conn.close()
