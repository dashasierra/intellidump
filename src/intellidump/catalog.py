"""Reading tables, keys and foreign keys from ``pg_catalog``."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from .errors import IntellidumpError
from .models import Catalog, Column, ExternalForeignKey, ForeignKey, Table

log = logging.getLogger(__name__)

#: PostgreSQL 11: pg_constraint.conparentid, which marks the copies of a foreign
#: key that partitions inherit from their partitioned table.
MIN_SERVER_VERSION = 110000
#: PostgreSQL 12 added generated columns (pg_attribute.attgenerated).
GENERATED_COLUMNS_VERSION = 120000

_TABLES_SQL = """
SELECT c.oid::int8, n.nspname, c.relname, c.relkind
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%(schemas)s)
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
ORDER BY n.nspname, c.relname
"""

_COLUMNS_SQL = """
SELECT a.attrelid::int8,
       a.attname,
       a.attnum,
       -- Type that lookup values are cast to. Without a typmod, "character" and
       -- "bit" mean char(1)/bit(1) and would truncate, so use bpchar / keep bit(n).
       CASE
           WHEN t.typname = 'bpchar' THEN 'bpchar'
           WHEN t.typname = '_bpchar' THEN 'bpchar[]'
           WHEN t.typname IN ('bit', '_bit') THEN pg_catalog.format_type(a.atttypid, a.atttypmod)
           ELSE pg_catalog.format_type(a.atttypid, NULL)
       END,
       t.typcategory,
       a.attnotnull,
       {generated},
       a.attidentity,
       pg_catalog.pg_get_serial_sequence(a.attrelid::regclass::text, a.attname)
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
WHERE a.attrelid = ANY(%(oids)s::oid[])
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

_KEY_COLUMNS = """
ARRAY(SELECT a.attname
      FROM unnest({keys}) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_catalog.pg_attribute a ON a.attrelid = {rel} AND a.attnum = k.attnum
      ORDER BY k.ord)
"""

_KEYS_SQL = f"""
SELECT con.conrelid::int8, con.contype, con.conname,
       {_KEY_COLUMNS.format(keys="con.conkey", rel="con.conrelid")}
FROM pg_catalog.pg_constraint con
WHERE con.conrelid = ANY(%(oids)s::oid[])
  AND con.contype IN ('p', 'u')
ORDER BY con.conrelid, con.contype, con.conname
"""

_FOREIGN_KEYS_SQL = f"""
SELECT con.conname,
       con.conrelid::int8,
       con.conrelid::regclass::text,
       {_KEY_COLUMNS.format(keys="con.conkey", rel="con.conrelid")},
       con.confrelid::int8,
       con.confrelid::regclass::text,
       {_KEY_COLUMNS.format(keys="con.confkey", rel="con.confrelid")}
FROM pg_catalog.pg_constraint con
WHERE con.contype = 'f'
  AND con.conparentid = 0
  AND (con.conrelid = ANY(%(oids)s::oid[]) OR con.confrelid = ANY(%(oids)s::oid[]))
ORDER BY con.conrelid, con.conname
"""

_RELATIONS_SQL = """
SELECT c.oid::int8, n.nspname, c.relname, c.relkind, c.relispartition
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = ANY(%(oids)s::oid[])
"""


@dataclass(frozen=True)
class RelationInfo:
    """Basic facts about one relation from pg_class.

    Args:
        oid: Relation OID.
        schema: Schema name.
        name: Relation name.
        kind: ``pg_class.relkind``, e.g. 'r' (table) or 'p' (partitioned table).
        is_partition: Whether it is a partition of another table.
    """

    oid: int
    schema: str
    name: str
    kind: str
    is_partition: bool

    @property
    def display_name(self) -> str:
        """Schema-qualified name for messages.

        Returns:
            ``schema.name``, unquoted.
        """
        return f"{self.schema}.{self.name}"


def check_server_version(conn: psycopg.Connection[Any]) -> None:
    """Fail unless the server is PostgreSQL 11 or newer.

    Args:
        conn: Open connection to the source database.
    """
    if conn.info.server_version < MIN_SERVER_VERSION:
        raise IntellidumpError(
            f"PostgreSQL 11 or newer is required (server is {conn.info.server_version})"
        )


def _columns_sql(conn: psycopg.Connection[Any]) -> str:
    """The column query for this server.

    Args:
        conn: Open connection to the source database.

    Returns:
        _COLUMNS_SQL; before PostgreSQL 12, which has no generated columns, no
        column counts as generated.
    """
    generated = (
        "a.attgenerated <> ''" if conn.info.server_version >= GENERATED_COLUMNS_VERSION else "false"
    )
    return _COLUMNS_SQL.replace("{generated}", generated)


def describe_relations(
    conn: psycopg.Connection[Any], oids: Iterable[int]
) -> dict[int, RelationInfo]:
    """Look up relations by OID.

    Args:
        conn: Open connection to the source database.
        oids: Relation OIDs; duplicates are ignored.

    Returns:
        A RelationInfo for each OID that exists, keyed by OID.
    """
    oid_list = sorted(set(oids))
    if not oid_list:
        return {}
    with conn.cursor() as cur:
        cur.execute(_RELATIONS_SQL, {"oids": oid_list})
        return {r[0]: RelationInfo(*r) for r in cur.fetchall()}


def resolve_relation(conn: psycopg.Connection[Any], name: str) -> RelationInfo:
    """Resolve a table name the way a query would, using the search_path.

    Args:
        conn: Open connection to the source database.
        name: Table name, optionally schema-qualified and quoted.

    Returns:
        The table's RelationInfo.
    """
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT pg_catalog.to_regclass(%s)::oid::int8", (name,))
        except psycopg.errors.InvalidName as exc:
            raise IntellidumpError(f"invalid table name {name!r}: {exc}") from None
        row = cur.fetchone()
    if row is None or row[0] is None:
        raise IntellidumpError(f"table {name!r} does not exist (or is not on the search_path)")
    return describe_relations(conn, [row[0]])[row[0]]


def load_catalog(conn: psycopg.Connection[Any], schemas: Sequence[str]) -> Catalog:
    """Load the tables, keys and foreign keys of the dumped schemas.

    Args:
        conn: Open connection to the source database.
        schemas: Schemas to load; duplicates are ignored.

    Returns:
        The catalog, including foreign keys that cross the schema boundary.
    """
    check_server_version(conn)
    schema_list = list(dict.fromkeys(schemas))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname = ANY(%s)", (schema_list,)
        )
        found = {r[0] for r in cur.fetchall()}
        missing = [s for s in schema_list if s not in found]
        if missing:
            raise IntellidumpError(f"schema(s) not found: {', '.join(missing)}")

        cur.execute(_TABLES_SQL, {"schemas": schema_list})
        table_rows = cur.fetchall()
        oids = [r[0] for r in table_rows]

        columns: dict[int, list[Column]] = defaultdict(list)
        if oids:
            cur.execute(_columns_sql(conn), {"oids": oids})
            for (
                relid,
                name,
                attnum,
                type_name,
                category,
                not_null,
                generated,
                identity,
                seq,
            ) in cur.fetchall():
                columns[relid].append(
                    Column(
                        name=name,
                        attnum=attnum,
                        type_name=type_name,
                        category=category,
                        not_null=not_null,
                        generated=generated,
                        identity=identity or "",
                        sequence=seq,
                    )
                )

        primary: dict[int, tuple[str, ...]] = {}
        unique: dict[int, list[tuple[str, ...]]] = defaultdict(list)
        if oids:
            cur.execute(_KEYS_SQL, {"oids": oids})
            for relid, contype, _name, cols in cur.fetchall():
                if contype == "p":
                    primary[relid] = tuple(cols)
                else:
                    unique[relid].append(tuple(cols))

        tables = {
            oid: Table(
                oid=oid,
                schema=schema,
                name=name,
                kind=kind,
                columns=tuple(columns[oid]),
                primary_key=primary.get(oid, ()),
                unique_keys=tuple(unique.get(oid, ())),
            )
            for oid, schema, name, kind in table_rows
        }

        foreign_keys: list[ForeignKey] = []
        external: list[ExternalForeignKey] = []
        if oids:
            cur.execute(_FOREIGN_KEYS_SQL, {"oids": oids})
            for (
                name,
                child,
                child_name,
                child_cols,
                parent,
                parent_name,
                parent_cols,
            ) in cur.fetchall():
                if child in tables and parent in tables:
                    foreign_keys.append(
                        ForeignKey(
                            name=name,
                            child_oid=child,
                            child_columns=tuple(child_cols),
                            parent_oid=parent,
                            parent_columns=tuple(parent_cols),
                        )
                    )
                else:
                    external.append(
                        ExternalForeignKey(
                            name=name,
                            child=child_name,
                            parent=parent_name,
                            parent_outside=parent not in tables,
                        )
                    )

    log.info(
        "catalog: %d tables, %d foreign keys in schema(s) %s",
        len(tables),
        len(foreign_keys),
        ", ".join(schema_list),
    )
    return Catalog(
        schemas=tuple(schema_list),
        tables=tables,
        foreign_keys=foreign_keys,
        external_foreign_keys=external,
    )
