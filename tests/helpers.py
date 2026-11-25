"""In-memory catalog and row source for unit tests (no database needed)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from itertools import count

from intellidump.models import Catalog, Column, FetchedRow, ForeignKey, Row, Table

_oids = count(1000)


def col(
    name: str,
    *,
    type_name: str = "integer",
    category: str = "N",
    not_null: bool = False,
    generated: bool = False,
    identity: str = "",
    sequence: str | None = None,
) -> Column:
    return Column(
        name=name,
        attnum=0,
        type_name=type_name,
        category=category,
        not_null=not_null,
        generated=generated,
        identity=identity,
        sequence=sequence,
    )


def table(
    name: str, *columns: Column, pk: Sequence[str] = ("id",), schema: str = "public"
) -> Table:
    cols = tuple(
        Column(**{**c.__dict__, "attnum": i + 1, "not_null": c.not_null or c.name in pk})
        for i, c in enumerate(columns)
    )
    return Table(
        oid=next(_oids), schema=schema, name=name, kind="r", columns=cols, primary_key=tuple(pk)
    )


def fk(
    name: str,
    child: Table,
    child_cols: Sequence[str],
    parent: Table,
    parent_cols: Sequence[str] = ("id",),
) -> ForeignKey:
    return ForeignKey(
        name=name,
        child_oid=child.oid,
        child_columns=tuple(child_cols),
        parent_oid=parent.oid,
        parent_columns=tuple(parent_cols),
    )


def catalog(tables: Sequence[Table], fks: Sequence[ForeignKey]) -> Catalog:
    return Catalog(schemas=("public",), tables={t.oid: t for t in tables}, foreign_keys=list(fks))


def r(*values: object) -> Row:
    """Row from Python values, in PostgreSQL text form."""
    return tuple(None if v is None else str(v) for v in values)


class FakeSource:
    """A RowSource over in-memory rows."""

    def __init__(self, data: dict[Table, list[Row]]) -> None:
        self.data = data
        self.calls: list[tuple[str, tuple[str, ...], int]] = []
        self.requests: list[tuple[str, tuple[str, ...], set[tuple[str, ...]]]] = []

    def fetch(
        self,
        table: Table,
        match_columns: Sequence[str],
        values: Collection[tuple[str, ...]],
        *,
        with_matches: bool = False,
    ) -> list[FetchedRow]:
        wanted = set(values)
        self.calls.append((table.name, tuple(match_columns), len(values)))
        self.requests.append((table.name, tuple(match_columns), wanted))
        idx = table.indexes_of(match_columns)
        return [
            (*self._fetched(table, n, row), tuple(row[i] for i in idx))
            for n, row in enumerate(self.data.get(table, []))
            if tuple(row[i] for i in idx) in wanted
        ]

    @staticmethod
    def _fetched(table: Table, n: int, row: Row) -> FetchedRow:
        # Tables without a key are identified by (tableoid, ctid), like PgRowSource.
        return table.key_of(row, (str(table.oid), f"(0,{n + 1})")), row

    def seed(self, table: Table, predicate: object) -> list[FetchedRow]:
        return [
            self._fetched(table, n, row)
            for n, row in enumerate(self.data[table])
            if predicate(row)  # type: ignore[operator]
        ]
