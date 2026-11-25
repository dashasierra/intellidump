"""Fetching rows from PostgreSQL.

The traversal code only talks to a :class:`RowSource`, so it can be unit
tested against an in-memory fake. :class:`PgRowSource` is the real one.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
from psycopg import sql

from .errors import IntellidumpError
from .models import Catalog, FetchedRow, LinkedRow, Row, Table

if TYPE_CHECKING:
    from .session import Session

log = logging.getLogger(__name__)

_ALIAS = "_intellidump_q"
_TRAILING_SEMICOLONS = re.compile(r"[\s;]+\Z")


class RowSource(Protocol):
    """Anything that can fetch rows by column values."""

    def fetch(
        self,
        table: Table,
        match_columns: Sequence[str],
        values: Collection[tuple[str, ...]],
        *,
        with_matches: bool = False,
    ) -> Iterable[FetchedRow | LinkedRow]:
        """Fetch the rows whose columns match any of the given values.

        Args:
            table: Table to read.
            match_columns: Columns to match.
            values: Value tuples in text form, one value per match column.
            with_matches: Also return the lookup values each row matched.

        Returns:
            Matching ``(key, row)`` pairs, or ``(key, row, matched values)`` triples;
            may be lazy, and is consumed fully before the next call.
        """
        ...


def normalize_query(query: str) -> str:
    """Strip whitespace and trailing semicolons from the query.

    Args:
        query: The user's SELECT.

    Returns:
        The cleaned query.
    """
    cleaned = _TRAILING_SEMICOLONS.sub("", query.strip())
    if not cleaned:
        raise IntellidumpError("the query is empty")
    return cleaned


def _wrapped(query: str) -> sql.Composed:
    """Parenthesize the query for use as a subquery.

    Args:
        query: The normalized query.

    Returns:
        ``(query)``, with a newline so a trailing ``--`` comment can't swallow the bracket.
    """
    # The newline before ")" keeps a trailing "-- comment" from eating it.
    return sql.SQL("(\n") + sql.SQL(query) + sql.SQL("\n)")


@dataclass(frozen=True)
class QueryShape:
    """Where each output column of the user's query comes from.

    Args:
        origins: ``(table OID, attnum)`` per output column; ``(0, 0)`` for expressions.
        names: Output column names.
    """

    origins: tuple[tuple[int, int], ...]
    names: tuple[str, ...] = ()

    @property
    def source_table_oids(self) -> list[int]:
        """Tables the query's output columns come from.

        Returns:
            Distinct table OIDs, sorted.
        """
        return sorted({oid for oid, _ in self.origins if oid})

    def position_of(self, table_oid: int, attnum: int) -> int | None:
        """Find the output column that is a given table column.

        Args:
            table_oid: Table OID.
            attnum: Column number in that table.

        Returns:
            Zero-based output position, or None.
        """
        for i, origin in enumerate(self.origins):
            if origin == (table_oid, attnum):
                return i
        return None

    def position_by_name(self, name: str) -> int | None:
        """Find the only output column with a given name.

        Args:
            name: Column name, matched exactly.

        Returns:
            Zero-based output position, or None if there is no such column or
            more than one.
        """
        hits = [i for i, n in enumerate(self.names) if n == name]
        return hits[0] if len(hits) == 1 else None


def probe_query(conn: psycopg.Connection[Any], query: str) -> QueryShape:
    """Describe the query's output columns without fetching any rows.

    Args:
        conn: Connection inside the dump snapshot.
        query: The normalized query.

    Returns:
        The query's shape.
    """
    stmt = (
        sql.SQL("SELECT * FROM ")
        + _wrapped(query)
        + sql.SQL(" AS {} LIMIT 0").format(sql.Identifier(_ALIAS))
    )
    with conn.cursor() as cur:
        try:
            cur.execute(stmt)
        except psycopg.errors.SyntaxError as exc:
            raise IntellidumpError(
                f"the query must be a single SELECT statement: {exc}".rstrip()
            ) from None
        res = cur.pgresult
        assert res is not None
        origins = tuple((res.ftable(i), res.ftablecol(i)) for i in range(res.nfields))
        names = tuple(col.name for col in cur.description or ())
    if not origins:
        raise IntellidumpError("the query returns no columns")
    return QueryShape(origins, names)


class PgRowSource:
    """Fetches rows from PostgreSQL, streaming results in chunks.

    A lookup whose connection is lost runs again on a new connection in the
    same snapshot (see :mod:`intellidump.session`), so it returns the same rows;
    rows it had already returned come again, and the store ignores repeats.
    """

    def __init__(
        self,
        session: Session,
        catalog: Catalog,
        batch_size: int = 1000,
        fetch_size: int = 1000,
    ) -> None:
        """Set up fetching over the dump's session.

        Args:
            session: The dump's connections, inside its snapshot.
            catalog: Tables being dumped.
            batch_size: Key values per lookup query.
            fetch_size: Rows per network round trip while streaming a result.
        """
        if batch_size < 1 or fetch_size < 1:
            raise ValueError("batch_size and fetch_size must be positive")
        self.session = session
        self.catalog = catalog
        self.batch_size = batch_size
        self.fetch_size = fetch_size
        self.queries = 0

    # -- SQL building -----------------------------------------------------

    @staticmethod
    def _relation(table: Table) -> sql.Composable:
        """FROM item for a table, aliased ``t``.

        Args:
            table: Table to read.

        Returns:
            ``ONLY schema.table AS t``, without ONLY for partitioned tables.
        """
        name = sql.Identifier(table.schema, table.name)
        # ONLY: rows of inheritance children belong to those tables. A
        # partitioned table, by contrast, holds its rows in its partitions.
        prefix = sql.SQL("") if table.kind == "p" else sql.SQL("ONLY ")
        return prefix + name + sql.SQL(" AS t")

    @staticmethod
    def _select_list(table: Table) -> sql.Composable:
        """Select list returning every column as text.

        Args:
            table: Table to read.

        Returns:
            The columns as text, plus tableoid and ctid for tables without a key.
        """
        items: list[sql.Composable] = [
            sql.SQL("CAST({} AS text)").format(sql.Identifier("t", c.name)) for c in table.columns
        ]
        if table.uses_physical_key:
            items.append(sql.SQL("CAST(t.tableoid AS oid)::text"))
            items.append(sql.SQL("t.ctid::text"))
        return sql.SQL(", ").join(items)

    def _stream(
        self, table: Table, stmt: sql.Composed, params: Any = None, *, resumable: bool = True
    ) -> Iterator[LinkedRow]:
        """Run a query and stream its rows, running it again if the connection is lost.

        Args:
            table: Table the query reads.
            stmt: The query: the select list, then any lookup values it matched.
            params: Query parameters.
            resumable: Run the query again after a lost connection. When False the
                error propagates, and the dump starts over.

        Yields:
            ``(key, row, matched lookup values)`` per result row; after a lost
            connection, rows already yielded can come again.
        """
        for attempt in itertools.count():
            try:
                yield from self._run(table, stmt, params)
                return
            except psycopg.Error as exc:
                if not (resumable and self.session.recover(exc, attempt)):
                    raise

    def _run(self, table: Table, stmt: sql.Composed, params: Any) -> Iterator[LinkedRow]:
        """Run a query once and stream its rows fetch_size at a time.

        Args:
            table: Table the query reads.
            stmt: The query: the select list, then any lookup values it matched.
            params: Query parameters.

        Yields:
            ``(key, row, matched lookup values)`` per result row.
        """
        self.queries += 1
        width = len(table.columns)
        extra = width + (2 if table.uses_physical_key else 0)
        # A raw cursor takes $n placeholders and never scans the text for "%", so
        # identifiers such as "%s" can't be mistaken for parameters.
        with psycopg.RawCursor(self.session.conn) as cur:
            try:
                records = cur.stream(stmt, params, size=self.fetch_size)
                first = next(records, None)
            except psycopg.NotSupportedError:
                # Chunked mode needs libpq 17+; single-row mode works everywhere.
                self.fetch_size = 1
                records = cur.stream(stmt, params, size=1)
                first = next(records, None)
            if first is None:
                return
            for rec in itertools.chain([first], records):
                row: Row = tuple(rec[:width])
                yield table.key_of(row, rec[width:extra]), row, tuple(rec[extra:])

    # -- RowSource ----------------------------------------------------------

    def fetch(
        self,
        table: Table,
        match_columns: Sequence[str],
        values: Collection[tuple[str, ...]],
        *,
        with_matches: bool = False,
    ) -> Iterator[LinkedRow]:
        """Fetch the rows whose columns match any of the given values.

        Args:
            table: Table to read.
            match_columns: Columns to match.
            values: Value tuples in text form, one value per match column.
            with_matches: Also return the lookup values each row matched.

        Yields:
            ``(key, row, the lookup values it matched, or ())``.
        """
        if not values:
            return
        cols = [table.column(c) for c in match_columns]
        knames = [sql.Identifier(f"k{i}") for i in range(len(cols))]
        stmt = sql.SQL(
            "SELECT {select}{matched} FROM {rel} JOIN unnest({arrays}) AS k({knames}) ON {cond}"
        ).format(
            select=self._select_list(table),
            matched=sql.SQL(", ") + sql.SQL(", ").join(knames) if with_matches else sql.SQL(""),
            rel=self._relation(table),
            arrays=sql.SQL(", ").join(sql.SQL(f"${i + 1}::text[]") for i in range(len(cols))),
            knames=sql.SQL(", ").join(knames),
            cond=sql.SQL(" AND ").join(
                sql.SQL("{} = CAST({} AS {})").format(
                    sql.Identifier("t", c.name),
                    sql.Identifier("k", f"k{i}"),
                    sql.SQL(c.type_name),
                )
                for i, c in enumerate(cols)
            ),
        )
        values = list(values)
        for start in range(0, len(values), self.batch_size):
            chunk = values[start : start + self.batch_size]
            params = [[v[i] for v in chunk] for i in range(len(cols))]
            yield from self._stream(table, stmt, params)
        log.debug(
            "looked up %s by (%s) for %d value(s)",
            table.display_name,
            ", ".join(match_columns),
            len(values),
        )

    def fetch_seed(
        self,
        table: Table,
        key_columns: Sequence[str],
        key_positions: Sequence[int],
        query: str,
        query_width: int,
    ) -> Iterator[LinkedRow]:
        """Fetch the rows of the root table that the user's query selects.

        Args:
            table: The root table.
            key_columns: Its key columns.
            key_positions: Where those columns appear in the query output.
            query: The normalized query.
            query_width: Number of output columns of the query.

        Returns:
            A lazy stream of ``(key, row, ())``. A lost connection is not retried
            here: running the query again later could select other rows (it may
            use ``now()``), so the dump starts over instead.
        """
        aliases = [sql.Identifier(f"c{i}") for i in range(query_width)]
        stmt = sql.SQL(
            "SELECT {select} FROM {rel} WHERE ({tcols}) IN "
            "(SELECT {qcols} FROM {query} AS {alias}({aliases}))"
        ).format(
            select=self._select_list(table),
            rel=self._relation(table),
            tcols=sql.SQL(", ").join(sql.Identifier("t", c) for c in key_columns),
            qcols=sql.SQL(", ").join(sql.Identifier(_ALIAS, f"c{p}") for p in key_positions),
            query=_wrapped(query),
            alias=sql.Identifier(_ALIAS),
            aliases=sql.SQL(", ").join(aliases),
        )
        return self._stream(table, stmt, resumable=False)
