"""The end-to-end dump: query -> traversal -> ordering -> ``.sql`` file."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .catalog import check_server_version, describe_relations, load_catalog, resolve_relation
from .collector import Collector
from .connection import ConnectionOptions, Connector, connect, copied_settings, reconnector
from .errors import IntellidumpError
from .models import Catalog, Table
from .ordering import Plan, build_plan, iter_plan
from .session import Session, describe_loss
from .source import PgRowSource, QueryShape, normalize_query, probe_query
from .store import DEFAULT_CACHE_MB, RowStore
from .writer import SchemaMap, WriteOptions, header_lines, write_file

log = logging.getLogger(__name__)


@dataclass
class DumpOptions:
    """What to dump and how.

    Args:
        query: SELECT choosing the rows to export.
        output: Output .sql file.
        connection: How to reach the source database.
        schemas: Schemas to traverse; empty means the schema of the query's table.
        table: Table the query's rows belong to, when it can't be inferred.
        exclude_tables: Tables never exported or traversed.
        follow_children: Export rows that reference the selected rows.
        follow_parents: Export rows that exported rows reference.
        on_conflict_do_nothing: Add ON CONFLICT DO NOTHING to each INSERT.
        transaction: Wrap the script in BEGIN/COMMIT.
        rows_per_insert: Rows per INSERT statement.
        set_sequences: Advance serial/identity sequences past the imported values.
        output_schemas: ``--output-schema`` values: ``TARGET`` or ``SOURCE=TARGET``.
        batch_size: Key values per lookup query.
        fetch_size: Rows per network round trip when streaming from PostgreSQL.
        max_rows: Abort if more rows than this would be exported; None for no limit.
        work_dir: Directory for the work database; the system temp directory if None.
        cache_mb: Work database page cache in MB, the main control over memory use.
        retries: After a lost connection, times to reconnect and run the interrupted
            query again in the same snapshot, and times to start the whole dump over
            when the snapshot is lost too. 0 uses a single connection and fails on
            the first lost connection.
        reconnect_timeout: Seconds to keep trying to reconnect after a loss.
    """

    query: str
    output: str | os.PathLike[str]
    connection: ConnectionOptions = field(default_factory=ConnectionOptions)
    schemas: Sequence[str] = ()
    table: str | None = None
    exclude_tables: Sequence[str] = ()
    follow_children: bool = True
    follow_parents: bool = True
    on_conflict_do_nothing: bool = False
    transaction: bool = False
    rows_per_insert: int = 1
    set_sequences: bool = False
    output_schemas: Sequence[str] = ()
    batch_size: int = 1000
    fetch_size: int = 1000
    max_rows: int | None = None
    work_dir: str | os.PathLike[str] | None = None
    cache_mb: int = DEFAULT_CACHE_MB
    retries: int = 5
    reconnect_timeout: float = 300.0


@dataclass
class DumpResult:
    """Summary of a finished dump.

    Args:
        root: Table the query selected from.
        plan: The insert plan that was written.
        counts: ``(table, rows)`` pairs in insert order.
        queries: Number of row queries sent to PostgreSQL.
        reconnects: Times a lost connection was replaced within the snapshot.
        restarts: Times the dump started over because the snapshot was lost.
    """

    root: Table
    plan: Plan
    counts: list[tuple[Table, int]]
    queries: int
    reconnects: int = 0
    restarts: int = 0

    @property
    def total_rows(self) -> int:
        """Number of rows written.

        Returns:
            Sum of the per-table counts.
        """
        return sum(n for _, n in self.counts)


def _seed_key(
    table: Table, shape: QueryShape, *, by_name: bool = False
) -> tuple[tuple[str, ...], list[int]] | None:
    """Find a unique key of the table that the query output fully contains.

    Args:
        table: Candidate root table.
        shape: Origins and names of the query's output columns.
        by_name: Also accept an output column whose origin is unknown (UNION,
            casts) when its name is the key column's name.

    Returns:
        ``(key columns, their positions in the query output)``, or None.
    """
    keys = [k for k in (table.primary_key, *table.unique_keys) if k]
    for key in keys:
        positions = [shape.position_of(table.oid, table.column(c).attnum) for c in key]
        if all(p is not None for p in positions):
            return key, [p for p in positions if p is not None]
    if by_name:
        for key in keys:
            positions = [shape.position_by_name(c) for c in key]
            if all(p is not None for p in positions):
                return key, [p for p in positions if p is not None]
    return None


def _missing_key_message(table: Table) -> str:
    """Explain that the query output lacks the table's key.

    Args:
        table: The query's table.

    Returns:
        Error message text.
    """
    if table.primary_key:
        cols = ", ".join(table.primary_key)
        return (
            f"the query's output must include the primary key of {table.display_name} "
            f"({cols}) so its rows can be identified; e.g. SELECT * or SELECT {cols}"
        )
    return (
        f"{table.display_name} has no primary key; the query's output must include all "
        "columns of one of its unique constraints"
    )


def _choose_root(
    conn: psycopg.Connection[Any], shape: QueryShape, options: DumpOptions
) -> tuple[Catalog, Table, tuple[str, ...], list[int]]:
    """Work out which table the query selects from and load the catalog.

    Args:
        conn: Connection inside the dump snapshot.
        shape: Origins of the query's output columns.
        options: Dump options; ``table`` and ``schemas`` are used.

    Returns:
        ``(catalog, root table, seed key columns, their query output positions)``.
    """
    if options.table:
        rel = resolve_relation(conn, options.table)
        candidates = [rel.oid]
    else:
        candidates = shape.source_table_oids
        if not candidates:
            raise IntellidumpError(
                "could not tell which table the query selects from (no output column is a "
                "plain table column); use --table"
            )
    rels = describe_relations(conn, candidates)
    for rel in rels.values():
        if rel.is_partition:
            raise IntellidumpError(
                f"{rel.display_name} is a partition; query its partitioned parent table instead"
            )
        if rel.kind not in ("r", "p"):
            raise IntellidumpError(f"{rel.display_name} is not a table")

    schemas = list(options.schemas) or sorted({rels[o].schema for o in candidates})
    catalog = load_catalog(conn, schemas)

    in_scope = [catalog.tables[o] for o in candidates if o in catalog.tables]
    if not in_scope:
        names = ", ".join(rels[o].display_name for o in candidates)
        raise IntellidumpError(
            f"the query's table ({names}) is not in the dumped schema(s) {', '.join(schemas)}"
        )

    by_name = options.table is not None
    keyed = [(t, k) for t in in_scope if (k := _seed_key(t, shape, by_name=by_name)) is not None]
    if len(in_scope) == 1 and not keyed:
        raise IntellidumpError(_missing_key_message(in_scope[0]))
    if not keyed:
        names = ", ".join(t.display_name for t in in_scope)
        raise IntellidumpError(
            f"the query reads from {names} but its output contains no table's full primary "
            "key; select the key columns of the table to export and/or use --table"
        )
    if len(keyed) > 1:
        names = ", ".join(t.display_name for t, _ in keyed)
        raise IntellidumpError(
            f"the query's output contains rows of several tables ({names}); "
            "use --table to say which one to export"
        )
    (root, (key, positions)) = keyed[0]
    if not options.schemas and len(schemas) > 1:
        catalog = load_catalog(conn, [root.schema])
        root = catalog.tables[root.oid]
    return catalog, root, key, positions


def _counts(plan: Plan, store: RowStore) -> list[tuple[Table, int]]:
    """Row counts per table in insert order.

    Args:
        plan: The insert plan.
        store: Work database holding the rows.

    Returns:
        ``(table, rows)`` pairs.
    """
    return [(t, store.counts[t.oid]) for t in plan.table_order]


def _restore(conn: psycopg.Connection[Any], saved: tuple[bool, Any, Any]) -> None:
    """Hand the caller's connection back the way it came, if it still works.

    Args:
        conn: The caller's connection.
        saved: Its ``(autocommit, isolation_level, read_only)`` before the dump.
    """
    if conn.closed or conn.broken:
        return
    # Nothing was written; the rollback also undoes the dump's settings.
    conn.rollback()
    conn.isolation_level, conn.read_only, conn.autocommit = saved[1], saved[2], saved[0]


def run_dump_on(
    conn: psycopg.Connection[Any], options: DumpOptions, *, connect: Connector | None = None
) -> DumpResult:
    """Run a dump over an open connection.

    Unless ``options.retries`` is 0, a second connection is opened (with
    ``connect``) so that a lost connection can be replaced in the same snapshot;
    see :mod:`intellidump.session`.

    Args:
        conn: Connection to the source database; its settings are restored afterwards.
        options: What to dump and how; ``connection`` is ignored.
        connect: Opens more connections to the same database; by default they
            use ``conn``'s parameters and password. Settings ``conn`` changed with
            SET are copied to them.

    Returns:
        Summary of what was written.
    """
    from . import __version__

    check_server_version(conn)
    query = normalize_query(options.query)
    schema_map = SchemaMap.parse(options.output_schemas)
    saved = (conn.autocommit, conn.isolation_level, conn.read_only)
    conn.rollback()
    settings = copied_settings(conn) if options.retries else []
    # Built now: by the time it's needed, conn may be broken.
    reopen = connect or reconnector(conn)
    restarts = 0
    while True:
        with RowStore(work_dir=options.work_dir, cache_mb=options.cache_mb) as store:
            session = Session(
                reopen,
                retries=options.retries,
                reconnect_timeout=options.reconnect_timeout,
                first=conn,
                settings=settings,
            )
            try:
                session.start()
                catalog, root, queries = _collect(session, options, query, schema_map, store)
            except Exception as exc:
                if restarts >= options.retries or not session.can_restart(exc):
                    raise
                restarts += 1
                log.warning(
                    "%s; starting the dump over in a new snapshot (restart %d of %d)",
                    describe_loss(exc),
                    restarts,
                    options.retries,
                )
                continue
            finally:
                session.close()
                _restore(conn, saved)
            return _finish(
                options,
                query,
                schema_map,
                store,
                session,
                catalog,
                root,
                queries,
                restarts,
                __version__,
            )


def _finish(
    options: DumpOptions,
    query: str,
    schema_map: SchemaMap,
    store: RowStore,
    session: Session,
    catalog: Catalog,
    root: Table,
    queries: int,
    restarts: int,
    version: str,
) -> DumpResult:
    """Order the collected rows and write the file: the work that needs no connection.

    Args:
        options: Dump options.
        query: The normalized query.
        schema_map: Output schema mapping.
        store: Work database holding the collected rows.
        session: The finished session, for the header and statistics.
        catalog: Tables and foreign keys.
        root: The query's table.
        queries: Number of row queries sent.
        restarts: Times the dump started over.
        version: intellidump version for the header.

    Returns:
        Summary of what was written.
    """
    plan = build_plan(catalog, store)
    schema_map.check(catalog.schemas, plan.table_order)
    counts = _counts(plan, store)
    write_options = WriteOptions(
        on_conflict_do_nothing=options.on_conflict_do_nothing,
        transaction=options.transaction,
        rows_per_insert=options.rows_per_insert,
        set_sequences=options.set_sequences,
        schemas=schema_map,
        header=header_lines(
            query=query,
            root=root,
            host=session.origin[0],
            port=session.origin[1],
            dbname=session.origin[2],
            schemas=catalog.schemas,
            counts=counts,
            version=version,
            output_schemas=schema_map,
        ),
    )
    write_file(options.output, plan, iter_plan(plan, store), write_options)

    updates = plan.update_count
    log.info(
        "wrote %d row(s) from %d table(s)%s to %s",
        sum(n for _, n in counts),
        len(counts),
        f" plus {updates} cycle-restoring UPDATE(s)" if updates else "",
        os.fspath(options.output),
    )
    return DumpResult(
        root=root,
        plan=plan,
        counts=counts,
        queries=queries,
        reconnects=session.reconnects,
        restarts=restarts,
    )


def _collect(
    session: Session,
    options: DumpOptions,
    query: str,
    schema_map: SchemaMap,
    store: RowStore,
) -> tuple[Catalog, Table, int]:
    """Find and traverse the rows: all the work that reads PostgreSQL.

    Args:
        session: The dump's connections, inside its snapshot.
        options: Dump options.
        query: The normalized query.
        schema_map: Output schema mapping, checked against the catalog.
        store: Work database to collect into.

    Returns:
        ``(catalog, root table, number of row queries)``.
    """
    session.set_search_path(options.schemas)
    conn = session.conn
    shape = probe_query(conn, query)
    catalog, root, key, positions = _choose_root(conn, shape, options)
    schema_map.check(catalog.schemas, ())

    excluded: set[int] = set()
    for name in options.exclude_tables:
        rel = resolve_relation(conn, name)
        if rel.oid == root.oid:
            raise IntellidumpError(f"cannot exclude the query's own table {rel.display_name}")
        excluded.add(rel.oid)

    for ext in catalog.external_foreign_keys:
        if ext.parent_outside:
            log.warning(
                "foreign key %s on %s references %s outside the dumped schema(s); "
                "those rows are not exported and must already exist in the target",
                ext.name,
                ext.child,
                ext.parent,
            )
        else:
            log.info(
                "foreign key %s on %s (outside the dumped schema(s)) references %s; "
                "its rows are not exported",
                ext.name,
                ext.child,
                ext.parent,
            )

    source = PgRowSource(
        session, catalog, batch_size=options.batch_size, fetch_size=options.fetch_size
    )
    collector = Collector(
        catalog,
        source,
        store,
        follow_children=options.follow_children,
        follow_parents=options.follow_parents,
        excluded=excluded,
        max_rows=options.max_rows,
        batch_size=options.batch_size,
    )
    seeded = collector.add_seed(
        root, source.fetch_seed(root, key, positions, query, len(shape.origins))
    )
    if not seeded:
        log.warning("the query matched no rows; the dump will contain no data")
    collector.run()
    return catalog, root, source.queries


def run_dump(options: DumpOptions) -> DumpResult:
    """Connect and run a dump.

    Args:
        options: What to dump and how, including ``connection``.

    Returns:
        Summary of what was written.
    """
    with connect(options.connection) as conn:
        # A prompted password is kept in options.connection, so reconnecting
        # doesn't ask again.
        return run_dump_on(conn, options, connect=lambda: connect(options.connection))
