"""Rendering a :class:`~intellidump.ordering.Plan` as a ``.sql`` script."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain, groupby
from pathlib import Path
from typing import TextIO

from .errors import IntellidumpError
from .models import Column, Row, Table, Value
from .ordering import Event, Insert, Plan, Update
from .store import chunked

_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NEEDS_ESCAPE = re.compile(r"[\\\x00-\x1f\x7f]")
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def quote_ident(name: str) -> str:
    """Quote an SQL identifier.

    Args:
        name: The identifier.

    Returns:
        The name in double quotes with inner quotes doubled, or a ``U&"..."``
        identifier with Unicode escapes if it contains control characters.
    """
    if _CONTROL.search(name):
        # Keeps the file one statement per line, like quote_literal. U&"" reads
        # the same whatever standard_conforming_strings is.
        body = name.replace("\\", "\\\\").replace('"', '""')
        body = _CONTROL.sub(lambda m: f"\\{ord(m.group()):04X}", body)
        return 'U&"' + body + '"'
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class SchemaMap:
    """Which schema each source schema's tables are written to.

    Args:
        default: Target for schemas not in ``mapping``; None keeps the source name.
        mapping: Source schema to target schema.
    """

    default: str | None = None
    mapping: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, values: Iterable[str]) -> SchemaMap:
        """Parse ``--output-schema`` values.

        Args:
            values: Each either ``TARGET`` or ``SOURCE=TARGET``.

        Returns:
            The schema map.
        """
        default: str | None = None
        mapping: dict[str, str] = {}
        for value in values:
            source, sep, target = value.partition("=")
            if not sep:
                source, target = "", value
            if not target or (sep and not source):
                raise IntellidumpError(
                    f"invalid --output-schema {value!r}: use TARGET or SOURCE=TARGET"
                )
            if not sep:
                if default is not None and default != target:
                    raise IntellidumpError(
                        "--output-schema TARGET (without SOURCE=) can only be given once"
                    )
                default = target
            elif mapping.get(source, target) != target:
                raise IntellidumpError(f"--output-schema maps schema {source!r} twice")
            else:
                mapping[source] = target
        return cls(default, mapping)

    def __call__(self, schema: str) -> str:
        """Target schema for a source schema.

        Args:
            schema: Source schema.

        Returns:
            Schema to write to.
        """
        return self.mapping.get(schema, self.default or schema)

    @property
    def is_identity(self) -> bool:
        """Whether every schema keeps its name.

        Returns:
            True if the map changes nothing.
        """
        return self.default is None and all(k == v for k, v in self.mapping.items())

    def check(self, source_schemas: Sequence[str], tables: Iterable[Table]) -> None:
        """Reject mappings for schemas not being dumped, and name collisions.

        Args:
            source_schemas: Schemas being dumped.
            tables: Tables that will be written.
        """
        unknown = sorted(set(self.mapping) - set(source_schemas))
        if unknown:
            raise IntellidumpError(
                f"--output-schema maps schema(s) not being dumped: {', '.join(unknown)} "
                f"(dumped: {', '.join(source_schemas)})"
            )
        seen: dict[tuple[str, str], Table] = {}
        for table in tables:
            target = (self(table.schema), table.name)
            other = seen.setdefault(target, table)
            if other is not table:
                raise IntellidumpError(
                    f"--output-schema would write both {other.display_name} and "
                    f"{table.display_name} to {target[0]}.{target[1]}"
                )


def qualified(table: Table, schemas: SchemaMap | None = None) -> str:
    """Quoted, schema-qualified table name.

    Args:
        table: The table.
        schemas: Output schema map; the source schema is used if None.

    Returns:
        ``"schema"."table"``.
    """
    schema = schemas(table.schema) if schemas else table.schema
    return f"{quote_ident(schema)}.{quote_ident(table.name)}"


def _escape_control(match: re.Match[str]) -> str:
    r"""Escape sequence for one control character inside an E'' literal.

    Args:
        match: Match of a single control character.

    Returns:
        For example ``\n`` or ``\x01``.
    """
    char = match.group()
    return _ESCAPES.get(char) or f"\\x{ord(char):02x}"


def quote_literal(value: str) -> str:
    """Quote an SQL string literal.

    Args:
        value: The text.

    Returns:
        The text in single quotes with inner quotes doubled, or an ``E'...'``
        literal with escapes if it contains control characters.
    """
    if _NEEDS_ESCAPE.search(value):
        # E'' literals read the same whatever standard_conforming_strings is, even
        # when a tool parses the whole file before running its SET statements. And
        # raw CR/LF/TAB would break the one-statement-per-line layout: anything
        # that rewrites line endings (git autocrlf, editors) would change the data.
        body = value.replace("\\", "\\\\").replace("'", "''")
        return "E'" + _CONTROL.sub(_escape_control, body) + "'"
    # No backslash: plain literals mean the same under either setting.
    return "'" + value.replace("'", "''") + "'"


def literal(value: Value, column: Column) -> str:
    """SQL literal for a value in PostgreSQL text form.

    Args:
        value: The value, or None for NULL.
        column: Its column; numbers and booleans are written unquoted.

    Returns:
        An untyped literal that PostgreSQL coerces to the column's type.
    """
    # Untyped like pg_dump --inserts, which works for every type with a text
    # input function: arrays, ranges, enums, json, domains, and so on.
    if value is None:
        return "NULL"
    # "-0" unquoted would be read as integer zero, losing a float's negative sign.
    negative_zero = value.startswith("-") and _NUMBER.fullmatch(value) and float(value) == 0
    if column.category == "N" and _NUMBER.fullmatch(value) and not negative_zero:
        return value
    if column.category == "B" and value in ("true", "false"):
        return value
    return quote_literal(value)


def _comment(text: str) -> str:
    """Turn text into SQL comment lines.

    Args:
        text: One or more lines.

    Returns:
        Each line prefixed with ``-- ``.
    """
    return "\n".join(f"-- {line}".rstrip() for line in text.splitlines() or [""])


@dataclass
class WriteOptions:
    """How to write the script.

    Args:
        on_conflict_do_nothing: Add ON CONFLICT DO NOTHING to each INSERT.
        transaction: Wrap the script in BEGIN/COMMIT.
        rows_per_insert: Rows per INSERT statement.
        set_sequences: Advance serial/identity sequences past the imported values.
        schemas: Schema each table is written to.
        header: Lines for the header comment.
    """

    on_conflict_do_nothing: bool = False
    transaction: bool = False
    rows_per_insert: int = 1
    set_sequences: bool = False
    schemas: SchemaMap = field(default_factory=SchemaMap)
    header: Sequence[str] = field(default_factory=list)


class SqlWriter:
    """Writes a plan as an SQL script."""

    def __init__(self, out: TextIO, options: WriteOptions) -> None:
        """Set up writing to a text stream.

        Args:
            out: Stream to write to.
            options: How to write the script.
        """
        if options.rows_per_insert < 1:
            raise ValueError("rows_per_insert must be at least 1")
        self.out = out
        self.options = options

    def _qualified(self, table: Table) -> str:
        """Output name of a table.

        Args:
            table: The table.

        Returns:
            Its quoted name in the output schema.
        """
        return qualified(table, self.options.schemas)

    def _w(self, text: str = "") -> None:
        """Write one line.

        Args:
            text: The line, without a newline.
        """
        self.out.write(text + "\n")

    def write(self, plan: Plan, events: Iterable[Event]) -> None:
        """Write the whole script.

        Args:
            plan: The plan; its warnings and table order are used.
            events: The plan's statements, consumed as a stream.
        """
        opts = self.options
        self._w("--")
        self._w("-- Intellidump data export")
        for line in opts.header:
            self._w(_comment(line) if line else "--")
        self._w("--")
        self._w()
        self._w("SET statement_timeout = 0;")
        self._w("SET lock_timeout = 0;")
        self._w("SET client_encoding = 'UTF8';")
        self._w("SET standard_conforming_strings = on;")
        self._w("SET check_function_bodies = false;")
        self._w("SET client_min_messages = warning;")
        self._w("SET xmloption = content;")
        self._w("SET idle_in_transaction_session_timeout = 0;")
        # Like pg_dump: nothing below resolves through a schema the target's users
        # control. Every name written is schema-qualified.
        self._w("SELECT pg_catalog.set_config('search_path', '', false);")
        self._w()
        if opts.transaction:
            self._w("BEGIN;")
            if plan.warnings:
                self._w("SET CONSTRAINTS ALL DEFERRED;")
            self._w()
        for warning in plan.warnings:
            self._w(_comment(f"WARNING: {warning}"))
            self._w()

        updates_section = -1  # table oids are positive
        section: int | None = None
        # Runs of consecutive events of one kind and table, read lazily.
        for (is_insert, oid), run in groupby(
            events, key=lambda ev: (isinstance(ev, Insert), ev.table.oid)
        ):
            if is_insert:
                first = next(run)
                if section != oid:
                    self._w()
                    # Names may contain newlines: never write them raw.
                    self._w(_comment(f"Data for {first.table.display_name}"))
                    self._w()
                    section = oid
                rows = chain([first], run)  # noqa: B031 - each run is read exactly once
                self._write_inserts(first.table, rows)  # type: ignore[arg-type]
            else:
                if section != updates_section:
                    self._w()
                    self._w("-- Restore foreign keys deferred to break reference cycles")
                    self._w()
                    section = updates_section
                for update in run:
                    self._write_update(update)  # type: ignore[arg-type]

        if opts.set_sequences:
            self._write_sequences(plan.table_order)

        if opts.transaction:
            self._w()
            self._w("COMMIT;")
        self._w()
        self._w("-- Intellidump export complete")

    def _write_inserts(self, table: Table, inserts: Iterable[Insert]) -> None:
        """Write INSERTs for consecutive rows of one table.

        Args:
            table: The table.
            inserts: Its rows, in order.
        """
        cols = table.insert_columns
        idx = table.indexes_of(c.name for c in cols)
        head = (
            f"INSERT INTO {self._qualified(table)} ({', '.join(quote_ident(c.name) for c in cols)})"
        )
        if table.needs_overriding_system_value:
            head += " OVERRIDING SYSTEM VALUE"
        tail = " ON CONFLICT DO NOTHING;" if self.options.on_conflict_do_nothing else ";"
        if not cols:  # every column is generated
            for _ in inserts:
                self._w(f"INSERT INTO {self._qualified(table)} DEFAULT VALUES{tail}")
            return
        n = self.options.rows_per_insert
        for chunk in chunked(inserts, n):
            tuples = [self._values(ins.row, cols, idx) for ins in chunk]
            if n == 1:
                self._w(f"{head} VALUES {tuples[0]}{tail}")
            else:
                self._w(f"{head} VALUES\n\t" + ",\n\t".join(tuples) + tail)

    @staticmethod
    def _values(row: Row, cols: Iterable[Column], idx: Sequence[int]) -> str:
        """Render one row as a VALUES tuple.

        Args:
            row: Row values.
            cols: Columns written.
            idx: Position of each written column in the row.

        Returns:
            ``(v1, v2, ...)``.
        """
        return "(" + ", ".join(literal(row[i], c) for i, c in zip(idx, cols, strict=True)) + ")"

    def _write_update(self, step: Update) -> None:
        """Write one cycle-restoring UPDATE.

        Args:
            step: The update.
        """
        table = step.table
        sets = ", ".join(
            f"{quote_ident(c)} = {literal(v, table.column(c))}" for c, v in step.assignments
        )
        where = " AND ".join(
            f"{quote_ident(c)} = {literal(v, table.column(c))}" for c, v in step.where
        )
        self._w(f"UPDATE {self._qualified(table)} SET {sets} WHERE {where};")

    def _write_sequences(self, tables: Sequence[Table]) -> None:
        """Write statements that move sequences past the imported values.

        Args:
            tables: Tables in insert order.
        """
        lines = []
        for table in tables:
            for col in table.columns:
                if not col.sequence:
                    continue
                target = self._qualified(table)
                # Look the sequence up on the target table at import time, and only
                # ever move it forward: next value = last_value + 1 once used,
                # otherwise its start value.
                lines.append(
                    "SELECT pg_catalog.setval(q.seq, q.v) FROM ("
                    f"SELECT pg_catalog.pg_get_serial_sequence({quote_literal(target)}, "
                    f"{quote_literal(col.name)})::regclass AS seq, "
                    f"(SELECT max({quote_ident(col.name)}) FROM {target}) AS v) q "
                    "JOIN pg_catalog.pg_sequence ps ON ps.seqrelid = q.seq "
                    "WHERE q.v IS NOT NULL AND q.v >= "
                    "COALESCE(pg_catalog.pg_sequence_last_value(q.seq) + 1, ps.seqstart);"
                )
        if lines:
            self._w()
            self._w("-- Advance sequences past the imported values")
            self._w()
            for line in lines:
                self._w(line)


def header_lines(
    *,
    query: str,
    root: Table,
    host: str | None,
    port: int | None,
    dbname: str | None,
    schemas: Sequence[str],
    counts: Sequence[tuple[Table, int]],
    version: str,
    output_schemas: SchemaMap | None = None,
) -> list[str]:
    """Lines describing the dump, for the header comment.

    Args:
        query: The normalized query.
        root: Table the query selected from.
        host: Source server host.
        port: Source server port.
        dbname: Source database.
        schemas: Dumped schemas.
        counts: ``(table, rows)`` pairs in insert order.
        version: Intellidump version.
        output_schemas: Output schema map, shown if it renames anything.

    Returns:
        The lines, without comment markers.
    """
    total = sum(n for _, n in counts)
    lines = [
        f"Generated by intellidump {version} at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Source: {host or 'local socket'}:{port or ''} database {dbname or ''}".rstrip(),
        f"Schema(s): {', '.join(schemas)}",
        *(
            ["Written to schema(s): " + ", ".join(f"{s} -> {output_schemas(s)}" for s in schemas)]
            if output_schemas and not output_schemas.is_identity
            else []
        ),
        f"Root table: {root.display_name}",
        "Query:",
        *("    " + line for line in query.splitlines()),
        "",
        f"Rows: {total} in {len(counts)} table(s), in insert order:",
        *(f"    {t.display_name}: {n}" for t, n in counts),
    ]
    return lines


def write_file(
    path: str | os.PathLike[str], plan: Plan, events: Iterable[Event], options: WriteOptions
) -> None:
    """Write the script to a file atomically.

    Args:
        path: Output file; its directory is created if needed.
        plan: The plan.
        events: The plan's statements, consumed as a stream.
        options: How to write the script.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            SqlWriter(fh, options).write(plan, events)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
