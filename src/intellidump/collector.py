"""Recursive foreign key traversal.

Starting from the rows selected by the query (the *seed* rows), two closures
are computed until nothing new turns up:

* **children** (dependents): rows in other tables whose foreign keys point at
  a seed row, then rows pointing at those rows, and so on. Only seed rows and
  their descendants are expanded this way.
* **parents** (references): every row that any collected row points at
  through a foreign key, recursively. Rows pulled in only as a parent do not
  get their own children expanded, otherwise a single shared parent (say, the
  ``user`` who owns everything) would drag the whole database along.

Both directions only follow foreign keys whose tables lie in the dumped
schemas. All state lives in the :class:`~intellidump.store.RowStore`, so
memory use does not grow with the size of the dump.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from .errors import IntellidumpError
from .models import Catalog, FetchedRow, ForeignKey, LinkedRow, Table
from .source import RowSource
from .store import RowStore

log = logging.getLogger(__name__)


#: Types whose equal values always have identical text, so ordering rows by
#: comparing text is exact for them. Anything else (numeric scale, citext,
#: interval, collations, ...) gets lookup matches recorded instead.
_TEXT_EXACT_TYPES = {"smallint", "integer", "bigint", "oid", "uuid", "date", "boolean"}


def _link_targets(catalog: Catalog) -> set[tuple[int, tuple[str, ...]]]:
    """Lookups whose matches must be recorded for ordering reference cycles.

    Args:
        catalog: Tables and foreign keys.

    Returns:
        ``(table OID, columns)`` pairs: both ends of every foreign key between
        tables in reference cycles whose columns are not all of a text-exact type.
    """
    cyclic = _cyclic_tables(catalog)
    out: set[tuple[int, tuple[str, ...]]] = set()
    for fk in catalog.foreign_keys:
        if fk.child_oid not in cyclic or fk.parent_oid not in cyclic:
            continue
        child, parent = catalog.table(fk.child_oid), catalog.table(fk.parent_oid)
        types = [child.column(c).type_name for c in fk.child_columns]
        types += [parent.column(c).type_name for c in fk.parent_columns]
        if any(t not in _TEXT_EXACT_TYPES for t in types):
            out.add((fk.child_oid, fk.child_columns))
            out.add((fk.parent_oid, fk.parent_columns))
    return out


def _cyclic_tables(catalog: Catalog) -> set[int]:
    """Tables that take part in a reference cycle, self-references included.

    Args:
        catalog: Tables and foreign keys.

    Returns:
        Their OIDs.
    """
    from .ordering import strongly_connected

    children: dict[int, list[int]] = {}
    for fk in catalog.foreign_keys:
        children.setdefault(fk.parent_oid, []).append(fk.child_oid)
    out = {fk.child_oid for fk in catalog.foreign_keys if fk.is_self_reference}
    for comp in strongly_connected(sorted(catalog.tables), lambda o: children.get(o, [])):
        if len(comp) > 1:
            out.update(comp)
    return out


class Collector:
    """Collects the seed rows and the rows linked to them through foreign keys."""

    def __init__(
        self,
        catalog: Catalog,
        source: RowSource,
        store: RowStore,
        *,
        follow_children: bool = True,
        follow_parents: bool = True,
        excluded: Iterable[int] = (),
        max_rows: int | None = None,
        batch_size: int = 1000,
    ) -> None:
        """Set up a traversal.

        Args:
            catalog: Tables and foreign keys to follow.
            source: Where rows are fetched from.
            store: Where collected rows and traversal state are kept.
            follow_children: Collect rows that reference seed rows or their descendants.
            follow_parents: Collect rows that collected rows reference.
            excluded: OIDs of tables that are never collected or traversed.
            max_rows: Abort once more rows than this are collected; None for no limit.
            batch_size: Key values per lookup.
        """
        self.catalog = catalog
        self.source = source
        self.store = store
        self.follow_children = follow_children
        self.follow_parents = follow_parents
        self.excluded = set(excluded)
        self.max_rows = max_rows
        self.batch_size = batch_size
        self._link_targets = _link_targets(catalog)

    # -- public API -----------------------------------------------------------

    def add_seed(self, table: Table, rows: Iterable[FetchedRow | LinkedRow]) -> int:
        """Add the rows selected by the query.

        Args:
            table: The query's table.
            rows: Its selected rows.

        Returns:
            Number of new rows stored.
        """
        if table.oid in self.excluded:
            raise IntellidumpError(f"the query's table {table.display_name} is excluded")
        added = self._ingest(table, rows, descendant=True)
        log.info("query selected %d row(s) from %s", added, table.display_name)
        return added

    def run(self) -> int:
        """Follow foreign keys until no new rows turn up.

        Returns:
            Total number of rows collected.
        """
        rounds = 0
        while True:
            busy = False
            if self.follow_children:
                busy |= self._expand("cq", to_children=True)
            if self.follow_parents:
                busy |= self._expand("pq", to_children=False)
            if not busy:
                break
            rounds += 1
            log.info("round %d: %d row(s) so far", rounds, self.store.total)
        self.store.commit()
        log.info("traversal finished after %d round(s): %d row(s)", rounds, self.store.total)
        return self.store.total

    # -- internals ------------------------------------------------------------

    def _ingest(
        self,
        table: Table,
        rows: Iterable[FetchedRow | LinkedRow],
        *,
        descendant: bool,
        link_columns: Sequence[str] | None = None,
    ) -> int:
        """Store fetched rows and enforce max_rows.

        Args:
            table: Table the rows belong to.
            rows: Fetched rows.
            descendant: Whether the rows depend on seed rows, so their own dependents
                get collected too.
            link_columns: Record which lookup values the rows matched on these columns.

        Returns:
            Number of new rows stored.
        """
        added = self.store.ingest(table, rows, descendant=descendant, link_columns=link_columns)
        if self.max_rows is not None and self.store.total > self.max_rows:
            raise IntellidumpError(
                f"more than {self.max_rows} rows collected (limit set by --max-rows); "
                "narrow the query or exclude tables with --exclude-table"
            )
        return added

    def _expand(self, queue: str, *, to_children: bool) -> bool:
        """Process one work queue of every table once.

        Args:
            queue: 'cq' (look up children) or 'pq' (look up parents).
            to_children: True when processing 'cq'.

        Returns:
            True if any rows were processed.
        """
        busy = False
        for oid in sorted(self.store.tables_with_rows()):
            table = self.catalog.table(oid)
            if not self.store.claim(table, queue):
                continue
            busy = True
            fks = (
                self.catalog.references_to(oid)
                if to_children
                else self.catalog.references_from(oid)
            )
            for fk in fks:
                self._follow(fk, queue, to_children=to_children)
            self.store.release(table, queue)
        return busy

    def _follow(self, fk: ForeignKey, queue: str, *, to_children: bool) -> None:
        """Fetch the rows linked through one foreign key to the rows being processed.

        Args:
            fk: Foreign key to follow.
            queue: Queue whose claimed rows are the starting points.
            to_children: Follow from parent rows to child rows, not the other way.
        """
        cat = self.catalog
        if to_children:
            src, dst = cat.table(fk.parent_oid), cat.table(fk.child_oid)
            src_cols, dst_cols = fk.parent_columns, fk.child_columns
        else:
            src, dst = cat.table(fk.child_oid), cat.table(fk.parent_oid)
            src_cols, dst_cols = fk.child_columns, fk.parent_columns
        if dst.oid in self.excluded:
            return
        added = 0
        for values in self.store.new_values(
            src,
            src_cols,
            queue,
            dst,
            dst_cols,
            descendant=to_children,
            batch_size=self.batch_size,
        ):
            # Rows in reference cycles are ordered row by row later, which needs to
            # know which rows PostgreSQL considers equal.
            link = (dst.oid, dst_cols) in self._link_targets
            added += self._ingest(
                dst,
                self.source.fetch(dst, dst_cols, values, with_matches=link),
                descendant=to_children,
                link_columns=dst_cols if link else None,
            )
        if added:
            log.debug(
                "%s %s -> %s via %s: %d new row(s)",
                "children" if to_children else "parents",
                src.display_name,
                dst.display_name,
                fk.name,
                added,
            )
