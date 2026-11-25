from __future__ import annotations

import io

import pytest

from helpers import col, r, table
from intellidump.errors import IntellidumpError
from intellidump.ordering import Insert, Plan, Update
from intellidump.writer import (
    SchemaMap,
    SqlWriter,
    WriteOptions,
    literal,
    quote_ident,
    write_file,
)


def InsertStep(table, rows):
    return [Insert(table, row) for row in rows]


def UpdateStep(table, where, assignments):
    return [Update(table, where, assignments)]


def P(steps=(), **kwargs):
    """A plan plus its event stream."""
    return Plan(**kwargs), [event for step in steps for event in step]


def render(plan_and_events, **opts) -> str:
    plan, events = plan_and_events
    buf = io.StringIO()
    SqlWriter(buf, WriteOptions(**opts)).write(plan, iter(events))
    return buf.getvalue()


@pytest.fixture
def people():
    return table(
        "people",
        col("id", identity="a", sequence="public.people_id_seq"),
        col("name", type_name="text", category="S"),
        col("active", type_name="boolean", category="B"),
        col("score", type_name="numeric", category="N"),
        col("upper_name", type_name="text", category="S", generated=True),
    )


@pytest.mark.parametrize(
    ("value", "category", "expected"),
    [
        (None, "S", "NULL"),
        ("O'Brien", "S", "'O''Brien'"),
        # Backslashes and control characters use E'' so the file means the same
        # whatever standard_conforming_strings is, and stays one statement per line.
        ("back\\slash", "S", "E'back\\\\slash'"),
        ("a\nb", "S", "E'a\\nb'"),
        ("cr\r\nlf\ttab", "S", "E'cr\\r\\nlf\\ttab'"),
        ("x\x01y\x7f", "S", "E'x\\x01y\\x7f'"),
        ("it's\n", "S", "E'it''s\\n'"),
        ("-0", "N", "'-0'"),
        ("-0.0", "N", "'-0.0'"),
        ("-1", "N", "-1"),
        ("42", "N", "42"),
        ("-1.5e10", "N", "-1.5e10"),
        ("NaN", "N", "'NaN'"),
        ("Infinity", "N", "'Infinity'"),
        ("$1.00", "N", "'$1.00'"),
        ("true", "B", "true"),
        ("{1,2}", "A", "'{1,2}'"),
        ('{"a": 1}', "U", "'{\"a\": 1}'"),
    ],
)
def test_literal(value, category, expected):
    assert literal(value, col("c", category=category)) == expected


def test_quote_ident():
    assert quote_ident('we"ird') == '"we""ird"'
    assert quote_ident("new\nline\\x") == r'U&"new\000Aline\\x"'


def test_insert_shape(people):
    plan = P(steps=[InsertStep(people, [r(1, "Ann", "true", "1.5", "ANN")])])
    sql = render(plan)
    assert (
        'INSERT INTO "public"."people" ("id", "name", "active", "score") '
        "OVERRIDING SYSTEM VALUE VALUES (1, 'Ann', true, 1.5);"
    ) in sql
    assert "ANN" not in sql  # generated column left out
    assert "BEGIN;" not in sql and "COMMIT;" not in sql
    assert "ON CONFLICT" not in sql
    assert "SET standard_conforming_strings = on;" in sql


def test_on_conflict_and_transaction(people):
    plan = P(steps=[InsertStep(people, [r(1, "Ann", None, None, None)])])
    sql = render(plan, on_conflict_do_nothing=True, transaction=True)
    assert "VALUES (1, 'Ann', NULL, NULL) ON CONFLICT DO NOTHING;" in sql
    assert sql.index("BEGIN;") < sql.index("INSERT") < sql.index("COMMIT;")


def test_rows_per_insert(people):
    rows = [r(i, f"p{i}", "false", i, None) for i in range(5)]
    sql = render(
        P(steps=[InsertStep(people, rows)]), rows_per_insert=2, on_conflict_do_nothing=True
    )
    assert sql.count("INSERT INTO") == 3
    assert "\t(0, 'p0', false, 0),\n\t(1, 'p1', false, 1) ON CONFLICT DO NOTHING;" in sql


def test_update_and_sequences(people):
    plan = P(
        steps=[
            InsertStep(people, [r(1, "Ann", None, None, None)]),
            UpdateStep(people, where=[("id", "1")], assignments=[("name", "Bob")]),
        ],
        table_order=[people],
    )
    sql = render(plan, set_sequences=True)
    assert 'UPDATE "public"."people" SET "name" = \'Bob\' WHERE "id" = 1;' in sql
    assert "pg_catalog.pg_get_serial_sequence('\"public\".\"people\"', 'id')::regclass" in sql
    assert sql.index("INSERT") < sql.index("UPDATE") < sql.index("setval")


def test_warnings_defer_constraints_in_transaction(people):
    plan = P(steps=[InsertStep(people, [r(1, "Ann", None, None, None)])], warnings=["cyc"])
    sql = render(plan, transaction=True)
    assert "SET CONSTRAINTS ALL DEFERRED;" in sql
    assert "-- WARNING: cyc" in sql


def test_header_comment_lines_are_prefixed(people):
    sql = render(P(), header=["Query:", "    SELECT 1\n    -- hi", ""])
    assert "--     SELECT 1\n--     -- hi\n--\n" in sql


def test_write_file_is_atomic(tmp_path, people):
    out = tmp_path / "sub" / "out.sql"
    write_file(out, *P(steps=[InsertStep(people, [r(1, "Ann", None, None, None)])]), WriteOptions())
    assert out.read_text(encoding="utf-8").startswith("--")
    assert [p.name for p in out.parent.iterdir()] == ["out.sql"]


def test_write_file_failure_leaves_no_file(tmp_path, people):
    out = tmp_path / "out.sql"
    with pytest.raises(ValueError):
        write_file(out, *P(), WriteOptions(rows_per_insert=0))
    assert list(tmp_path.iterdir()) == []


def test_output_schema_rewrites_every_statement(people):
    plan = P(
        steps=[
            InsertStep(people, [r(1, "Ann", None, None, None)]),
            UpdateStep(people, where=[("id", "1")], assignments=[("name", "Bob")]),
        ],
        table_order=[people],
    )
    sql = render(plan, set_sequences=True, schemas=SchemaMap.parse(["custom"]))
    assert 'INSERT INTO "custom"."people" ("id", "name", "active", "score")' in sql
    assert 'UPDATE "custom"."people" SET' in sql
    assert "pg_get_serial_sequence('\"custom\".\"people\"', 'id')" in sql
    assert 'FROM "custom"."people")' in sql
    assert '"public"' not in sql


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], {"public": "public", "audit": "audit"}),
        (["custom"], {"public": "custom", "audit": "custom"}),
        (["public=custom"], {"public": "custom", "audit": "audit"}),
        (["public=custom", "archive"], {"public": "custom", "audit": "archive"}),
        (["My Schema"], {"public": "My Schema", "audit": "My Schema"}),
    ],
)
def test_schema_map_parse(values, expected):
    m = SchemaMap.parse(values)
    assert {s: m(s) for s in expected} == expected


@pytest.mark.parametrize("values", [["a", "b"], ["=x"], ["x="], ["p=a", "p=b"]])
def test_schema_map_parse_rejects(values):
    with pytest.raises(IntellidumpError):
        SchemaMap.parse(values)


def test_schema_map_check():
    a = table("t", col("id"), schema="public")
    b = table("t", col("id"), schema="audit")
    SchemaMap.parse(["public=x"]).check(["public", "audit"], [a, b])
    with pytest.raises(IntellidumpError, match="both"):
        SchemaMap.parse(["custom"]).check(["public", "audit"], [a, b])
    with pytest.raises(IntellidumpError, match="not being dumped"):
        SchemaMap.parse(["nope=x"]).check(["public"], [a])
