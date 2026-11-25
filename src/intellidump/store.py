"""Disk-backed working storage for collected rows.

Nothing about the size of a dump is kept in Python memory. Every collected
row, the traversal bookkeeping (which rows still need their children or
parents looked up, which key values were already fetched) and the ordering
state live in a temporary SQLite database in the work directory. SQLite keeps
a bounded page cache in RAM (``cache_mb``) and spills everything else to
disk, so a dump can be much larger than the memory of the machine running it.

Each dumped table ``T`` gets a SQLite table ``r<oid>``:

* ``c0 .. cN``: the row's values in PostgreSQL text form. Integer columns are
  stored with INTEGER affinity so they sort numerically and stay compact.
* ``p0, p1``: ``tableoid`` and ``ctid`` for tables with no usable key.
* ``pq`` / ``cq``: work queues (0 = done, 1 = pending, 2 = being processed)
  for looking up the row's parents / children.
* ``ex``: the row's children are (to be) expanded, i.e. it is a seed row or a
  descendant of one rather than only somebody's parent.
* ``wave``, ``indeg``, ``nulled``: used when ordering reference cycles.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any, TypeVar

from .models import FetchedRow, Key, LinkedRow, Row, Table, Value

log = logging.getLogger(__name__)

T = TypeVar("T")

#: PostgreSQL types whose text form is a plain base-10 integer.
_INTEGER_TYPES = {"smallint", "integer", "bigint", "oid"}

DEFAULT_CACHE_MB = 256
INGEST_CHUNK = 1000


def chunked(items: Iterable[T], size: int) -> Iterator[list[T]]:
    """Split an iterable into lists.

    Args:
        items: Items to split.
        size: Maximum list length.

    Yields:
        Lists of up to ``size`` items.
    """
    it = iter(items)
    while chunk := list(islice(it, size)):
        yield chunk


def _text(value: Any) -> Value:
    """Convert a value read from SQLite back to PostgreSQL text form.

    Args:
        value: Stored value; integer columns come back as int.

    Returns:
        The value as text, or None.
    """
    if value is None or isinstance(value, str):
        return value
    return str(value)


@dataclass(frozen=True)
class _Rel:
    """How one PostgreSQL table is laid out in the work database.

    Args:
        name: SQLite table name.
        width: Number of PostgreSQL columns.
        key_cols: SQLite columns identifying a row.
        physical: Whether the key is (tableoid, ctid).
    """

    name: str
    width: int
    key_cols: tuple[str, ...]
    physical: bool

    @property
    def value_cols(self) -> str:
        """SQL list of the value columns.

        Returns:
            ``c0, c1, ...``.
        """
        return ", ".join(f"c{i}" for i in range(self.width))

    @property
    def key_list(self) -> str:
        """SQL list of the key columns.

        Returns:
            For example ``c0`` or ``p0, p1``.
        """
        return ", ".join(self.key_cols)

    @property
    def key_match(self) -> str:
        """SQL condition matching one row by key.

        Returns:
            For example ``c0 = ?``.
        """
        return " AND ".join(f"{c} = ?" for c in self.key_cols)


class RowStore:
    """Disk-backed SQLite work database for collected rows and traversal state."""

    def __init__(
        self, *, work_dir: str | os.PathLike[str] | None = None, cache_mb: int = DEFAULT_CACHE_MB
    ) -> None:
        """Create the work database file.

        Args:
            work_dir: Directory for the file; the system temp directory if None.
            cache_mb: SQLite page cache size in MB.
        """
        if cache_mb < 1:
            raise ValueError("cache_mb must be at least 1")
        if work_dir is not None:
            os.makedirs(work_dir, exist_ok=True)
        fd, self.path = tempfile.mkstemp(prefix="intellidump-", suffix=".sqlite", dir=work_dir)
        os.close(fd)
        self.db = sqlite3.connect(self.path)
        # A scratch database: no crash safety needed, so no journal and no fsync.
        for pragma in (
            "journal_mode = OFF",
            "synchronous = OFF",
            "locking_mode = EXCLUSIVE",
            "temp_store = FILE",
            f"cache_size = -{cache_mb * 1024}",
        ):
            self.db.execute(f"PRAGMA {pragma}")
        self._rels: dict[int, _Rel] = {}
        self._tables: dict[int, Table] = {}
        self._caches: dict[tuple[int, tuple[str, ...], bool], list[Any]] = {}
        self._links: dict[tuple[int, tuple[str, ...]], str] = {}
        self.counts: dict[int, int] = defaultdict(int)
        log.debug("work database: %s (cache %d MB)", self.path, cache_mb)

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        """Close and delete the work database."""
        with contextlib.suppress(Exception):
            self.db.close()
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def __enter__(self) -> RowStore:
        """Use the store as a context manager.

        Returns:
            The store itself.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Close and delete the work database.

        Args:
            *exc: Exception details, ignored.
        """
        self.close()

    def commit(self) -> None:
        """Commit pending writes to the work database."""
        self.db.commit()

    @property
    def total(self) -> int:
        """Rows collected so far.

        Returns:
            Total across all tables.
        """
        return sum(self.counts.values())

    # -- layout ------------------------------------------------------------------

    def rel(self, table: Table) -> _Rel:
        """Layout of a table in the work database, created on first use.

        Args:
            table: PostgreSQL table.

        Returns:
            Its layout.
        """
        rel = self._rels.get(table.oid)
        if rel is not None:
            return rel
        physical = table.uses_physical_key
        key_cols = (
            ("p0", "p1")
            if physical
            else tuple(f"c{i}" for i in table.indexes_of(table.row_key_columns))
        )
        rel = _Rel(f"r{table.oid}", len(table.columns), key_cols, physical)
        cols = [
            f"c{i} {'INTEGER' if c.type_name in _INTEGER_TYPES else 'TEXT'}"
            for i, c in enumerate(table.columns)
        ]
        if physical:
            cols += ["p0 TEXT", "p1 TEXT"]
        cols += [
            "pq INTEGER NOT NULL",
            "cq INTEGER NOT NULL",
            "ex INTEGER NOT NULL",
            "wave INTEGER",
            "indeg INTEGER",
            "nulled TEXT",
        ]
        self.db.execute(f"CREATE TABLE {rel.name} (id INTEGER PRIMARY KEY, {', '.join(cols)})")
        self.db.execute(f"CREATE UNIQUE INDEX {rel.name}_key ON {rel.name} ({rel.key_list})")
        self.db.execute(f"CREATE INDEX {rel.name}_pq ON {rel.name} (pq)")
        self.db.execute(f"CREATE INDEX {rel.name}_cq ON {rel.name} (cq)")
        self._rels[table.oid] = rel
        self._tables[table.oid] = table
        return rel

    def column_names(self, table: Table, columns: Sequence[str]) -> list[str]:
        """Work-database columns for table columns.

        Args:
            table: PostgreSQL table.
            columns: Its column names.

        Returns:
            SQLite column names such as ``c3``.
        """
        return [f"c{i}" for i in table.indexes_of(columns)]

    def ensure_index(self, table: Table, columns: Sequence[str]) -> None:
        """Create an index on table columns if there isn't one.

        Args:
            table: PostgreSQL table.
            columns: Columns to index.
        """
        rel = self.rel(table)
        cols = self.column_names(table, columns)
        name = f"{rel.name}_i_{'_'.join(c[1:] for c in cols)}"
        self.db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {rel.name} ({', '.join(cols)})")

    def tables_with_rows(self) -> list[int]:
        """Tables that have collected rows.

        Returns:
            Their OIDs.
        """
        return [oid for oid, n in self.counts.items() if n]

    # -- collecting --------------------------------------------------------------

    def ingest(
        self,
        table: Table,
        rows: Iterable[FetchedRow | LinkedRow],
        *,
        descendant: bool,
        link_columns: Sequence[str] | None = None,
    ) -> int:
        """Store fetched rows and queue them for traversal.

        Args:
            table: Table the rows belong to.
            rows: Rows to store; consumed in chunks.
            descendant: Also queue the rows for child lookup.
            link_columns: When set, also record which lookup value each row matched
                on these columns (rows must be ``(key, row, matched values)``).

        Returns:
            Number of rows that were new.
        """
        # New rows are queued for parent lookup, and for child lookup too when they
        # are descendants. A known row reached again as a descendant is queued for
        # child lookup if it was only somebody's parent so far.
        rel = self.rel(table)
        flag = 1 if descendant else 0
        marks = ", ".join("?" for _ in range(rel.width + (2 if rel.physical else 0)))
        cols = rel.value_cols + (", p0, p1" if rel.physical else "")
        insert = f"INSERT OR IGNORE INTO {rel.name} ({cols}, pq, cq, ex) VALUES ({marks}, 1, ?, ?)"
        upgrade = f"UPDATE {rel.name} SET ex = 1, cq = 1 WHERE ex = 0 AND {rel.key_match}"
        link = self._link_insert(table, link_columns) if link_columns else None
        added = 0
        for chunk in chunked(rows, INGEST_CHUNK):
            before = self.db.total_changes
            if rel.physical:
                self.db.executemany(insert, [(*it[1], *it[0], flag, flag) for it in chunk])
            else:
                self.db.executemany(insert, [(*it[1], flag, flag) for it in chunk])
            added += self.db.total_changes - before
            if descendant:
                self.db.executemany(upgrade, [tuple(it[0]) for it in chunk])
            if link:
                pairs = [(*it[2], *it[0]) for it in chunk if len(it) > 2]
                self.db.executemany(link, pairs)
        self.counts[table.oid] += added
        return added

    def _link_insert(self, table: Table, columns: Sequence[str]) -> str:
        """INSERT statement recording lookup matches, creating its table if needed.

        Args:
            table: Table whose rows were looked up.
            columns: Columns the lookup values were matched against.

        Returns:
            SQL inserting ``(matched values..., row key...)``.
        """
        rel = self.rel(table)
        key = (table.oid, tuple(columns))
        name = self._links.get(key)
        vcols = [f"v{i}" for i in range(len(columns))]
        kcols = [f"k{i}" for i in range(len(rel.key_cols))]
        if name is None:
            name = self._links[key] = f"l{len(self._links)}"
            cols = ", ".join(vcols + kcols)
            self.db.execute(f"CREATE TABLE {name} ({cols}, UNIQUE ({cols}))")
        marks = ", ".join("?" for _ in vcols + kcols)
        return f"INSERT OR IGNORE INTO {name} VALUES ({marks})"

    def links(self, table: Table, columns: Sequence[str]) -> str | None:
        """Table of recorded lookup matches, if any.

        Args:
            table: Table whose rows were looked up.
            columns: Columns the lookup values were matched against.

        Returns:
            SQLite table name with columns ``v0..`` (values matched, equal to the
            row's ``columns`` in PostgreSQL) and ``k0..`` (the row's key), or None.
        """
        return self._links.get((table.oid, tuple(columns)))

    def claim(self, table: Table, queue: str) -> int:
        """Mark the rows pending in a queue as being processed.

        Args:
            table: PostgreSQL table.
            queue: 'pq' (parent lookup) or 'cq' (child lookup).

        Returns:
            Number of rows claimed.
        """
        rel = self.rel(table)
        cur = self.db.execute(f"UPDATE {rel.name} SET {queue} = 2 WHERE {queue} = 1")
        return cur.rowcount

    def release(self, table: Table, queue: str) -> None:
        """Mark the claimed rows of a queue as done.

        Args:
            table: PostgreSQL table.
            queue: 'pq' or 'cq'.
        """
        rel = self.rel(table)
        self.db.execute(f"UPDATE {rel.name} SET {queue} = 0 WHERE {queue} = 2")
        self.commit()

    def new_values(
        self,
        src: Table,
        src_columns: Sequence[str],
        queue: str,
        dst: Table,
        dst_columns: Sequence[str],
        *,
        descendant: bool,
        batch_size: int,
    ) -> Iterator[list[tuple[str, ...]]]:
        """Key values to look up next, skipping those already looked up.

        Args:
            src: Table whose claimed rows supply the values.
            src_columns: Columns of ``src`` to read.
            queue: Queue the rows were claimed from.
            dst: Table the values will be looked up in.
            dst_columns: Columns of ``dst`` the values are matched against.
            descendant: Whether the lookup is for child rows.
            batch_size: Maximum values per batch.

        Yields:
            Batches of distinct, non-NULL value tuples in text form.
        """
        rel = self.rel(src)
        key = (dst.oid, tuple(dst_columns), descendant)
        cache = self._caches.get(key)
        width = len(src_columns)
        vcols = ", ".join(f"v{i}" for i in range(width))
        if cache is None:
            name = f"f{len(self._caches)}"
            self.db.execute(f"CREATE TABLE {name} ({vcols}, UNIQUE ({vcols}))")
            cache = self._caches[key] = [name, 0]  # [table, rowid watermark]
        name = cache[0]
        cols = self.column_names(src, src_columns)
        self.db.execute(
            f"INSERT OR IGNORE INTO {name} ({vcols}) SELECT DISTINCT {', '.join(cols)} "
            f"FROM {rel.name} WHERE {queue} = 2 AND "
            + " AND ".join(f"{c} IS NOT NULL" for c in cols)  # MATCH SIMPLE
        )
        while True:
            batch = self.db.execute(
                f"SELECT rowid, {vcols} FROM {name} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (cache[1], batch_size),
            ).fetchall()
            if not batch:
                return
            cache[1] = batch[-1][0]
            yield [tuple(str(v) for v in rec[1:]) for rec in batch]

    # -- reading -----------------------------------------------------------------

    def _row(self, rel: _Rel, rec: Sequence[Any], table: Table) -> Row:
        """Rebuild a row from a work-database record.

        Args:
            rel: The table's layout.
            rec: Record holding the value columns, then ``nulled``.
            table: PostgreSQL table.

        Returns:
            The row, with deferred foreign key columns set to None.
        """
        row = tuple(_text(v) for v in rec[: rel.width])
        nulled = rec[rel.width]
        if nulled:
            drop = set(table.indexes_of(json.loads(nulled)))
            row = tuple(None if i in drop else v for i, v in enumerate(row))
        return row

    def iter_keys(self, table: Table) -> Iterator[Key]:
        """Keys of all stored rows of a table.

        Args:
            table: PostgreSQL table.

        Yields:
            Keys in key order.
        """
        rel = self.rel(table)
        for rec in self.db.execute(
            f"SELECT {rel.key_list} FROM {rel.name} ORDER BY {rel.key_list}"
        ):
            yield tuple(_text(v) for v in rec)

    def iter_rows(self, table: Table) -> Iterator[Row]:
        """All stored rows of a table.

        Args:
            table: PostgreSQL table.

        Yields:
            Rows in key order; integer keys sort numerically.
        """
        rel = self.rel(table)
        cur = self.db.execute(
            f"SELECT {rel.value_cols}, nulled FROM {rel.name} ORDER BY {rel.key_list}"
        )
        for rec in cur:
            yield self._row(rel, rec, table)

    def iter_rows_by_wave(self, table: Table) -> Iterator[tuple[int, Row]]:
        """All stored rows of a cycle table, with their wave.

        Args:
            table: PostgreSQL table.

        Yields:
            ``(wave, row)`` in wave and key order.
        """
        rel = self.rel(table)
        cur = self.db.execute(
            f"SELECT {rel.value_cols}, nulled, wave FROM {rel.name} ORDER BY wave, {rel.key_list}"
        )
        for rec in cur:
            yield rec[-1], self._row(rel, rec, table)

    def iter_nulled(self, table: Table) -> Iterator[tuple[Row, list[str]]]:
        """Rows inserted with deferred foreign key columns.

        Args:
            table: PostgreSQL table.

        Yields:
            ``(original row, deferred column names)``.
        """
        rel = self.rel(table)
        cur = self.db.execute(
            f"SELECT {rel.value_cols}, nulled FROM {rel.name} WHERE nulled IS NOT NULL ORDER BY id"
        )
        for rec in cur:
            yield tuple(_text(v) for v in rec[: rel.width]), json.loads(rec[rel.width])

    def raw_row(self, table: Table, row_id: int) -> Row:
        """A stored row by id, with its original values.

        Args:
            table: PostgreSQL table.
            row_id: Work-database row id.

        Returns:
            The row, ignoring any deferred columns.
        """
        rel = self.rel(table)
        rec = self.db.execute(
            f"SELECT {rel.value_cols} FROM {rel.name} WHERE id = ?", (row_id,)
        ).fetchone()
        return tuple(_text(v) for v in rec)
