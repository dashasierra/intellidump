"""Putting collected rows in an order that satisfies every foreign key.

Tables are ordered topologically (referenced tables first). Tables that take
part in a reference cycle -- a self-referencing ``parent_id`` column, or two
tables pointing at each other -- are ordered row by row instead, in *waves*:
wave 0 holds the rows that reference no other collected row of the group,
wave 1 the rows referencing only wave-0 rows, and so on. If the *data* itself
is cyclic, the cycle is broken by inserting one row with the offending
nullable foreign key columns set to NULL and restoring them with an UPDATE
once the rows they point at exist.

Row-level ordering runs as set-based SQL inside the work database, so it
needs no memory proportional to the number of rows either.
"""

from __future__ import annotations

import heapq
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import NamedTuple, TypeVar

from .models import Catalog, ForeignKey, Row, Table, Value
from .store import RowStore

log = logging.getLogger(__name__)

N = TypeVar("N", bound=Hashable)

#: Cycles broken per stall before the wave loop resumes.
BREAKS_PER_STALL = 1000


class Insert(NamedTuple):
    """Statement event: insert one row.

    Args:
        table: Target table.
        row: Values in column order; deferred foreign key columns are None.
    """

    table: Table
    row: Row


class Update(NamedTuple):
    """Statement event: restore foreign key values NULLed to break a data cycle.

    Args:
        table: Target table.
        where: ``(column, value)`` pairs identifying the row.
        assignments: ``(column, value)`` pairs to set.
    """

    table: Table
    where: list[tuple[str, Value]]
    assignments: list[tuple[str, Value]]


Event = Insert | Update


@dataclass
class TableSegment:
    """Plan segment: all rows of one table, in key order.

    Args:
        table: The table.
    """

    table: Table


@dataclass
class CycleSegment:
    """Plan segment: rows of tables in a reference cycle, in wave order.

    Args:
        tables: Tables of the cycle, in output rank order.
        update_count: Rows inserted with deferred foreign keys.
    """

    tables: list[Table]
    update_count: int = 0


Segment = TableSegment | CycleSegment


@dataclass
class Plan:
    """The order to write collected rows in.

    Args:
        segments: Segments in insert order.
        table_order: Tables in insert order.
        warnings: Problems to report in the output file.
    """

    segments: list[Segment] = field(default_factory=list)
    table_order: list[Table] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def update_count(self) -> int:
        """Number of cycle-restoring UPDATEs.

        Returns:
            Total across all cycle segments.
        """
        return sum(s.update_count for s in self.segments if isinstance(s, CycleSegment))


def strongly_connected(nodes: Iterable[N], successors: Callable[[N], Iterable[N]]) -> list[list[N]]:
    """Find strongly connected components (Tarjan's algorithm, iterative).

    Args:
        nodes: Graph nodes, in the order to visit them.
        successors: Returns a node's successors.

    Returns:
        Components in reverse topological order.
    """
    index: dict[N, int] = {}
    low: dict[N, int] = {}
    on_stack: set[N] = set()
    stack: list[N] = []
    result: list[list[N]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[N, Iterator[N]]] = [(root, iter(successors(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(successors(nxt))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                result.append(comp)
    return result


class _CycleOrderer:
    """Assigns insert waves to the rows of one group of mutually referencing tables."""

    def __init__(
        self,
        catalog: Catalog,
        store: RowStore,
        tables: list[Table],
        fks: list[ForeignKey],
        plan: Plan,
    ) -> None:
        """Set up ordering for one group of tables.

        Args:
            catalog: Tables and foreign keys.
            store: Work database holding the rows.
            tables: Tables of the group, in output rank order.
            fks: Foreign keys between tables of the group.
            plan: Plan to add warnings to.
        """
        self.catalog = catalog
        self.store = store
        self.db = store.db
        self.tables = tables
        self.fks = fks
        self.plan = plan
        self.rank = {t.oid: i for i, t in enumerate(tables)}
        self.segment = CycleSegment(tables)

    def _r(self, oid: int) -> str:
        """Work-database table name for a table.

        Args:
            oid: Table OID.

        Returns:
            SQLite table name.
        """
        return self.store.rel(self.catalog.table(oid)).name

    def run(self) -> CycleSegment:
        """Assign every row a wave so referenced rows always come in earlier waves.

        Returns:
            The segment to add to the plan.
        """
        # Work tables: e holds one edge per (child row, parent row, foreign key);
        # fr is this wave's frontier and nf the next one. A row's indeg counts its
        # edges from parents that are not placed yet.
        db = self.db
        for name in ("e", "fr", "nf"):
            db.execute(f"DROP TABLE IF EXISTS {name}")
        db.execute(
            "CREATE TABLE e (ct INTEGER, cid INTEGER, pt INTEGER, pid INTEGER, f INTEGER, "
            "PRIMARY KEY (ct, cid, pt, pid, f)) WITHOUT ROWID"
        )
        db.execute("CREATE TABLE fr (t INTEGER, id INTEGER, PRIMARY KEY (t, id)) WITHOUT ROWID")
        db.execute("CREATE TABLE nf (t INTEGER, id INTEGER, PRIMARY KEY (t, id)) WITHOUT ROWID")

        # Edges parent row -> child row, for every foreign key inside the group.
        for f, fk in enumerate(self.fks):
            parent, child = self.catalog.table(fk.parent_oid), self.catalog.table(fk.child_oid)
            self.store.ensure_index(parent, fk.parent_columns)
            pcols = self.store.column_names(parent, fk.parent_columns)
            ccols = self.store.column_names(child, fk.child_columns)
            cond = " AND ".join(f"p.{p} = c.{c}" for p, c in zip(pcols, ccols, strict=True))
            if parent.oid == child.oid:
                cond += " AND p.id <> c.id"  # a row referencing itself is fine
            rc, rp = self._r(child.oid), self._r(parent.oid)
            insert = "INSERT OR IGNORE INTO e SELECT ?, c.id, ?, p.id, ? "
            args = (child.oid, parent.oid, f)
            same = " AND p.id <> c.id" if parent.oid == child.oid else ""
            db.execute(f"{insert}FROM {rc} c JOIN {rp} p ON {cond}", args)
            # Values PostgreSQL calls equal can differ as text ('B' and 'b' in citext,
            # '48 hours' and '2 days'), so also use the matches recorded by lookups.
            pkey, ckey = self.store.rel(parent).key_cols, self.store.rel(child).key_cols
            lp = self.store.links(parent, fk.parent_columns)
            if lp:
                on_v = " AND ".join(f"l.v{i} = c.{c}" for i, c in enumerate(ccols))
                on_k = " AND ".join(f"p.{k} = l.k{i}" for i, k in enumerate(pkey))
                db.execute(
                    f"{insert}FROM {rc} c JOIN {lp} l ON {on_v} JOIN {rp} p ON {on_k}{same}",
                    args,
                )
            lc = self.store.links(child, fk.child_columns)
            if lc:
                on_v = " AND ".join(f"l.v{i} = p.{p}" for i, p in enumerate(pcols))
                on_k = " AND ".join(f"c.{k} = l.k{i}" for i, k in enumerate(ckey))
                db.execute(
                    f"{insert}FROM {rp} p JOIN {lc} l ON {on_v} JOIN {rc} c ON {on_k}{same}",
                    args,
                )
        db.execute("CREATE INDEX e_p ON e (pt, pid)")

        total = 0
        for t in self.tables:
            r = self._r(t.oid)
            db.execute(
                f"UPDATE {r} SET wave = NULL, nulled = NULL, indeg = "
                f"(SELECT count(*) FROM e WHERE e.ct = ? AND e.cid = {r}.id)",
                (t.oid,),
            )
            db.execute(f"INSERT INTO fr SELECT ?, id FROM {r} WHERE indeg = 0", (t.oid,))
            total += self.store.counts[t.oid]

        children = "SELECT e.cid FROM fr JOIN e ON e.pt = fr.t AND e.pid = fr.id WHERE e.ct = ?"
        placed = 0
        wave = 0
        while placed < total:
            if db.execute("SELECT 1 FROM fr LIMIT 1").fetchone() is None:
                for oid, row_id in self._break_cycles():
                    db.execute(f"UPDATE {self._r(oid)} SET indeg = 0 WHERE id = ?", (row_id,))
                    db.execute("INSERT INTO fr VALUES (?, ?)", (oid, row_id))
            for t in self.tables:
                cur = db.execute(
                    f"UPDATE {self._r(t.oid)} SET wave = ? "
                    "WHERE id IN (SELECT id FROM fr WHERE t = ?)",
                    (wave, t.oid),
                )
                placed += cur.rowcount
            for t in self.tables:
                r = self._r(t.oid)
                db.execute(
                    f"UPDATE {r} SET indeg = indeg - (SELECT count(*) FROM fr JOIN e "
                    f"ON e.pt = fr.t AND e.pid = fr.id WHERE e.ct = ? AND e.cid = {r}.id) "
                    f"WHERE wave IS NULL AND id IN ({children})",
                    (t.oid, t.oid),
                )
                db.execute(
                    f"INSERT OR IGNORE INTO nf SELECT ?, id FROM {r} "
                    f"WHERE indeg = 0 AND wave IS NULL AND id IN ({children})",
                    (t.oid, t.oid),
                )
            db.execute("DELETE FROM fr")
            db.execute("INSERT INTO fr SELECT t, id FROM nf")
            db.execute("DELETE FROM nf")
            wave += 1
            if wave % 1000 == 0:
                log.info("ordering cycle rows: wave %d, %d of %d placed", wave, placed, total)

        for t in self.tables:
            r = self._r(t.oid)
            rel = self.store.rel(t)
            db.execute(f"CREATE INDEX IF NOT EXISTS {r}_wave ON {r} (wave, {rel.key_list})")
        for name in ("e", "fr", "nf"):
            db.execute(f"DROP TABLE {name}")
        self.store.commit()
        log.debug(
            "ordered %d row(s) of %s in %d wave(s)",
            total,
            ", ".join(t.display_name for t in self.tables),
            wave,
        )
        return self.segment

    # -- cycle breaking ------------------------------------------------------------

    def _state(self, t: int, row_id: int) -> tuple[bool, bool]:
        """Placement state of a row.

        Args:
            t: Table OID.
            row_id: Work-database row id.

        Returns:
            ``(unplaced, blocked)``: not in a wave yet / still waiting on a parent.
        """
        wave, indeg = self.db.execute(
            f"SELECT wave, indeg FROM {self._r(t)} WHERE id = ?", (row_id,)
        ).fetchone()
        return wave is None, wave is None and indeg > 0

    def _parents(self, t: int, row_id: int) -> list[tuple[int, int, int]]:
        """Edges from a row to the rows it references.

        Args:
            t: Table OID.
            row_id: Work-database row id.

        Returns:
            ``(parent table OID, parent row id, foreign key index)`` triples.
        """
        return self.db.execute(
            "SELECT pt, pid, f FROM e WHERE ct = ? AND cid = ? ORDER BY pt, pid, f", (t, row_id)
        ).fetchall()

    def _find_cycle(self, start: tuple[int, int]) -> list[tuple[int, int]] | None:
        """Walk from a blocked row through blocked parents until a row repeats.

        Args:
            start: ``(table OID, row id)`` of a blocked row.

        Returns:
            The cycle's rows sorted by rank and id, or None if the walk ends at rows
            whose cycles are already broken.
        """
        seen: dict[tuple[int, int], int] = {}
        path: list[tuple[int, int]] = []
        node = start
        while node not in seen:
            seen[node] = len(path)
            path.append(node)
            nxt = next(
                ((pt, pid) for pt, pid, _ in self._parents(*node) if self._state(pt, pid)[1]),
                None,
            )
            if nxt is None:
                return None
            node = nxt
        return sorted(path[seen[node] :], key=lambda n: (self.rank[n[0]], n[1]))

    def _try_break(self, cycle: list[tuple[int, int]]) -> tuple[int, int] | None:
        """Break a cycle by deferring nullable foreign keys of one of its rows.

        Args:
            cycle: Rows of the cycle, in order of preference.

        Returns:
            ``(table OID, row id)`` of the row broken, or None if none can be.
        """
        for t, row_id in cycle:
            table = self.catalog.table(t)
            if not table.row_key_columns:
                continue
            # NULL every reference to a row that isn't placed yet, including rows
            # broken in this same stall: those land in the same wave.
            cols: set[str] = set()
            for pt, pid, f in self._parents(t, row_id):
                if self._state(pt, pid)[0]:
                    cols.update(self.fks[f].child_columns)
            if cols & set(table.row_key_columns):
                continue
            if any(table.column(c).not_null for c in cols):
                continue
            ordered = sorted(cols, key=table.index_of)
            self.db.execute(
                f"UPDATE {self._r(t)} SET nulled = ?, indeg = 0 WHERE id = ?",
                (json.dumps(ordered), row_id),
            )
            self.segment.update_count += 1
            log.debug(
                "breaking reference cycle at %s row %d by deferring %s",
                table.display_name,
                row_id,
                ", ".join(ordered),
            )
            return t, row_id
        return None

    def _break_cycles(self) -> list[tuple[int, int]]:
        """Break up to BREAKS_PER_STALL cycles when no row is ready.

        Returns:
            Rows to place next: the broken ones, or one unbreakable row (with a
            warning) if no cycle could be broken.
        """
        broken: list[tuple[int, int]] = []
        unbreakable: list[tuple[int, int]] | None = None
        for t in self.tables:
            last = 0
            while len(broken) < BREAKS_PER_STALL:
                ids = self.db.execute(
                    f"SELECT id FROM {self._r(t.oid)} WHERE wave IS NULL AND indeg > 0 "
                    "AND id > ? ORDER BY id LIMIT 100",
                    (last,),
                ).fetchall()
                if not ids:
                    break
                for (row_id,) in ids:
                    last = row_id
                    if not self._state(t.oid, row_id)[1]:
                        continue
                    cycle = self._find_cycle((t.oid, row_id))
                    if cycle is None:
                        continue
                    node = self._try_break(cycle)
                    if node is None:
                        unbreakable = unbreakable or cycle
                        continue
                    broken.append(node)
                    if len(broken) >= BREAKS_PER_STALL:
                        break
        if broken:
            log.info("broke %d reference cycle(s) with deferred UPDATEs", len(broken))
            return broken

        assert unbreakable is not None
        oid, row_id = unbreakable[0]
        table = self.catalog.table(oid)
        values = self.store.raw_row(table, row_id)
        key = (
            ", ".join(f"{c}={values[table.index_of(c)]}" for c in table.row_key_columns)
            or f"#{row_id}"
        )
        msg = (
            f"rows in {table.display_name} form a reference cycle through NOT NULL foreign key "
            f"columns; row ({key}) is written before a row it references, so the import only "
            "succeeds if those constraints are DEFERRABLE (with --transaction the script then "
            "runs SET CONSTRAINTS ALL DEFERRED) or checks are disabled"
        )
        self.plan.warnings.append(msg)
        log.warning(msg)
        return [(oid, row_id)]


def build_plan(catalog: Catalog, store: RowStore) -> Plan:
    """Order the collected rows so every foreign key is satisfied.

    Args:
        catalog: Tables and foreign keys.
        store: Work database holding the rows.

    Returns:
        The plan.
    """
    plan = Plan()
    present = set(store.tables_with_rows())
    fks = [
        fk for fk in catalog.foreign_keys if fk.child_oid in present and fk.parent_oid in present
    ]
    children: dict[int, set[int]] = defaultdict(set)
    for fk in fks:
        children[fk.parent_oid].add(fk.child_oid)

    def table_rank(oid: int) -> tuple[str, str]:
        """Sort key for a table.

        Args:
            oid: Table OID.

        Returns:
            ``(schema, name)``.
        """
        return catalog.table(oid).sort_key

    comps = strongly_connected(
        sorted(present, key=table_rank), lambda o: sorted(children[o], key=table_rank)
    )
    comp_of = {oid: i for i, comp in enumerate(comps) for oid in comp}
    comp_children: dict[int, set[int]] = defaultdict(set)
    indegree = [0] * len(comps)
    for fk in fks:
        a, b = comp_of[fk.parent_oid], comp_of[fk.child_oid]
        if a != b and b not in comp_children[a]:
            comp_children[a].add(b)
            indegree[b] += 1

    def comp_rank(i: int) -> tuple[tuple[str, str], int]:
        """Sort key for a group of tables, so ties are broken by name.

        Args:
            i: Index of the group in ``comps``.

        Returns:
            ``(smallest table sort key, i)``.
        """
        return (min(table_rank(o) for o in comps[i]), i)

    heap = [comp_rank(i) for i in range(len(comps)) if indegree[i] == 0]
    heapq.heapify(heap)
    while heap:
        _, i = heapq.heappop(heap)
        tables = [catalog.table(o) for o in sorted(comps[i], key=table_rank)]
        plan.table_order.extend(tables)
        oids = set(comps[i])
        internal = [fk for fk in fks if fk.child_oid in oids and fk.parent_oid in oids]
        if internal:
            plan.segments.append(_CycleOrderer(catalog, store, tables, internal, plan).run())
        else:
            plan.segments.append(TableSegment(tables[0]))
        for j in comp_children[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                heapq.heappush(heap, comp_rank(j))
    return plan


def _ranked(store: RowStore, table: Table, rank: int) -> Iterator[tuple[int, int, Row]]:
    """Rows of one cycle table, tagged for merging.

    Args:
        store: Work database holding the rows.
        table: Table to read.
        rank: The table's position within its cycle segment.

    Yields:
        ``(wave, rank, row)`` in wave and key order.
    """
    for wave, row in store.iter_rows_by_wave(table):
        yield wave, rank, row


def iter_plan(plan: Plan, store: RowStore) -> Iterator[Event]:
    """Stream the statements of a plan in order.

    Args:
        plan: The plan.
        store: Work database holding the rows.

    Yields:
        Insert and Update events.
    """
    for seg in plan.segments:
        if isinstance(seg, TableSegment):
            for row in store.iter_rows(seg.table):
                yield Insert(seg.table, row)
            continue
        streams = [_ranked(store, t, rank) for rank, t in enumerate(seg.tables)]
        for _, rank, row in heapq.merge(*streams, key=lambda item: (item[0], item[1])):
            yield Insert(seg.tables[rank], row)
        if seg.update_count:
            for table in seg.tables:
                for row, cols in store.iter_nulled(table):
                    yield Update(
                        table,
                        where=[(c, row[table.index_of(c)]) for c in table.row_key_columns],
                        assignments=[(c, row[table.index_of(c)]) for c in cols],
                    )
