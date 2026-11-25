"""In-memory description of the tables and foreign keys being traversed.

Every value is kept in PostgreSQL's text representation (``col::text``), so
rows never pass through Python type conversion. That keeps the output exact
for any column type (numeric, arrays, ranges, json, enums, PostGIS, ...).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .errors import IntellidumpError

#: A single column value in PostgreSQL text form, or ``None`` for SQL NULL.
Value = str | None
#: One row: values in the table's column order.
Row = tuple[Value, ...]
#: Identity of a row inside its table (primary key values, or tableoid+ctid).
Key = tuple[Value, ...]
#: ``(key, row)`` as returned by a row source.
FetchedRow = tuple[Key, Row]
#: ``(key, row, matched values)``: a lookup result plus the lookup values it matched.
LinkedRow = tuple[Key, Row, tuple[str, ...]]


@dataclass(frozen=True)
class Column:
    """A table column.

    Args:
        name: Column name.
        attnum: ``pg_attribute.attnum``.
        type_name: Type to cast lookup values to, e.g. ``integer``; never one that
        truncates, so ``bpchar`` rather than ``character``.
        category: ``pg_type.typcategory``: 'N' numeric, 'B' boolean, 'S' string, ...
        not_null: Whether the column is NOT NULL.
        generated: Whether it is a generated column, which INSERTs leave out.
        identity: 'a' for GENERATED ALWAYS, 'd' for BY DEFAULT, '' for neither.
        sequence: Owned sequence (serial or identity) as a quoted name, or None.
    """

    name: str
    attnum: int
    type_name: str
    category: str
    not_null: bool
    generated: bool = False
    identity: str = ""
    sequence: str | None = None


@dataclass(frozen=True)
class ForeignKey:
    """A foreign key ``child(child_columns) REFERENCES parent(parent_columns)``.

    Args:
        name: Constraint name.
        child_oid: OID of the referencing table.
        child_columns: Referencing columns.
        parent_oid: OID of the referenced table.
        parent_columns: Referenced columns, matching ``child_columns`` by position.
    """

    name: str
    child_oid: int
    child_columns: tuple[str, ...]
    parent_oid: int
    parent_columns: tuple[str, ...]

    @property
    def is_self_reference(self) -> bool:
        """Whether the key references its own table.

        Returns:
            True for a self-reference such as ``parent_id``.
        """
        return self.child_oid == self.parent_oid


@dataclass(frozen=True)
class ExternalForeignKey:
    """A foreign key that crosses the boundary of the dumped schemas.

    Args:
        name: Constraint name.
        child: Referencing table, as a display name.
        parent: Referenced table, as a display name.
        parent_outside: Whether the referenced table is the one outside.
    """

    name: str
    child: str
    parent: str
    parent_outside: bool


@dataclass(eq=False)
class Table:
    """A table in the dumped schemas.

    Args:
        oid: Table OID.
        schema: Schema name.
        name: Table name.
        kind: ``pg_class.relkind``: 'r' ordinary table, 'p' partitioned table.
        columns: Columns in attnum order.
        primary_key: Primary key columns, or empty.
        unique_keys: Columns of each unique constraint.
    """

    oid: int
    schema: str
    name: str
    kind: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()
    _index: dict[str, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Index the columns by name."""
        self._index = {c.name: i for i, c in enumerate(self.columns)}

    @property
    def display_name(self) -> str:
        """Schema-qualified name for messages.

        Returns:
            ``schema.name``, unquoted.
        """
        return f"{self.schema}.{self.name}"

    @property
    def sort_key(self) -> tuple[str, str]:
        """Key for ordering tables deterministically.

        Returns:
            ``(schema, name)``.
        """
        return (self.schema, self.name)

    def column(self, name: str) -> Column:
        """Look up a column by name.

        Args:
            name: Column name.

        Returns:
            The column.
        """
        return self.columns[self.index_of(name)]

    def index_of(self, name: str) -> int:
        """Position of a column in the table's rows.

        Args:
            name: Column name.

        Returns:
            Zero-based index.
        """
        try:
            return self._index[name]
        except KeyError:
            raise IntellidumpError(f"column {name!r} not found in {self.display_name}") from None

    def indexes_of(self, names: Iterable[str]) -> tuple[int, ...]:
        """Positions of several columns in the table's rows.

        Args:
            names: Column names.

        Returns:
            Zero-based indexes, in the given order.
        """
        return tuple(self.index_of(n) for n in names)

    @property
    def row_key_columns(self) -> tuple[str, ...]:
        """Columns that identify a row: the primary key, else a NOT NULL unique key.

        Returns:
            Column names, or an empty tuple if the table has neither.
        """
        # Without a key, rows are identified by (tableoid, ctid), which stays
        # stable within the dump's REPEATABLE READ snapshot.
        if self.primary_key:
            return self.primary_key
        for key in self.unique_keys:
            if all(self.column(c).not_null for c in key):
                return key
        return ()

    @property
    def uses_physical_key(self) -> bool:
        """Whether rows are identified by tableoid and ctid.

        Returns:
            True if the table has no usable key.
        """
        return not self.row_key_columns

    @property
    def insert_columns(self) -> tuple[Column, ...]:
        """Columns written in INSERTs.

        Returns:
            All columns except generated ones.
        """
        return tuple(c for c in self.columns if not c.generated)

    @property
    def needs_overriding_system_value(self) -> bool:
        """Whether INSERTs need OVERRIDING SYSTEM VALUE.

        Returns:
            True if a GENERATED ALWAYS identity column is written.
        """
        return any(c.identity == "a" for c in self.insert_columns)

    def key_of(self, row: Row, extras: Sequence[Value] = ()) -> Key:
        """Identity of a row.

        Args:
            row: Row values in column order.
            extras: ``(tableoid, ctid)``, used when the table has no key.

        Returns:
            The key values.
        """
        if self.row_key_columns:
            return tuple(row[i] for i in self.indexes_of(self.row_key_columns))
        return tuple(extras)


@dataclass
class Catalog:
    """Tables and foreign keys of the dumped schemas.

    Args:
        schemas: The dumped schemas.
        tables: Tables keyed by OID.
        foreign_keys: Foreign keys with both tables in the dumped schemas.
        external_foreign_keys: Foreign keys crossing the schema boundary.
    """

    schemas: tuple[str, ...]
    tables: dict[int, Table]
    foreign_keys: list[ForeignKey]
    external_foreign_keys: list[ExternalForeignKey] = field(default_factory=list)
    outgoing: dict[int, list[ForeignKey]] = field(init=False, repr=False)
    incoming: dict[int, list[ForeignKey]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Index the foreign keys by child and by parent table."""
        out: dict[int, list[ForeignKey]] = defaultdict(list)
        inc: dict[int, list[ForeignKey]] = defaultdict(list)
        for fk in self.foreign_keys:
            out[fk.child_oid].append(fk)
            inc[fk.parent_oid].append(fk)
        self.outgoing = dict(out)
        self.incoming = dict(inc)

    def table(self, oid: int) -> Table:
        """Look up a table by OID.

        Args:
            oid: Table OID.

        Returns:
            The table.
        """
        return self.tables[oid]

    def references_from(self, oid: int) -> list[ForeignKey]:
        """Foreign keys declared on a table.

        Args:
            oid: OID of the referencing (child) table.

        Returns:
            Its foreign keys within the dumped schemas.
        """
        return self.outgoing.get(oid, [])

    def references_to(self, oid: int) -> list[ForeignKey]:
        """Foreign keys pointing at a table.

        Args:
            oid: OID of the referenced (parent) table.

        Returns:
            Foreign keys referencing it within the dumped schemas.
        """
        return self.incoming.get(oid, [])
