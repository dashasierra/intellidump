"""Dump, re-import into an empty database, and compare every value.

Each test computes the expected rows independently (see ``rt.Env.expected``),
dumps, replays the file into a separate target database with all constraints
enabled, and compares the target with the expectation row by row.
"""

from __future__ import annotations

import pytest

import rt
from rt import needs_db

pytestmark = [pytest.mark.integration, needs_db]

# (table suffix, column type, SQL expressions for the values)
TYPES: list[tuple[str, str, list[str]]] = [
    ("int2", "smallint", ["-32768", "32767", "0"]),
    ("int4", "integer", ["-2147483648", "2147483647"]),
    ("int8", "bigint", ["-9223372036854775808", "9223372036854775807"]),
    (
        "numeric",
        "numeric",
        [
            "0",
            "-0.000",
            "1.50",
            "'NaN'",
            "123456789012345678901234567890.123456789012345678901234567890",
            "0.000000000000000000000000000000000000001",
            "-1e-130",
        ],
    ),
    ("numeric_typmod", "numeric(7,3)", ["1.5", "-9999.999", "0"]),
    (
        "float8",
        "double precision",
        [
            "'NaN'",
            "'Infinity'",
            "'-Infinity'",
            "'-0'",
            "'0'",
            "'0.1'",
            "'1.7976931348623157e308'",
            "'5e-324'",
            "'2.2250738585072014e-308'",
            "'-123456.789e-10'",
        ],
    ),
    ("float4", "real", ["'NaN'", "'-0'", "'3.4028235e38'", "'1e-45'", "'0.1'", "'-Infinity'"]),
    ("money", "money", ["'-1234.56'", "'0'", "'92233720368547758.07'"]),
    (
        "text",
        "text",
        [
            "''",
            "'plain'",
            "'it''s'",
            "'\"double\"'",
            r"E'back\\slash'",
            r"E'\\\\'",
            r"E'line1\nline2'",
            r"E'cr\rlf\r\nend'",
            r"E'\ttab'",
            "'🙂 emoji, ñ, 中文, עברית'",
            "E'zero\\u200Bwidth \\u2028 line sep \\u2029 para'",
            "'$$ dollar $tag$'",
            "'-- not a comment'",
            "'/* not a comment */'",
            "'; DROP TABLE x; --'",
            r"E'\n\\! echo psql-meta\n'",
            r"E'\n\\i /nonexistent\n'",
            r"E'x\\gexec'",
            "repeat('long text ', 200000)",
            "'%s %(name)s {} {0}'",
        ],
    ),
    ("varchar", "varchar(8)", ["'12345678'", "''", "'a'"]),
    ("bpchar", "char(6)", ["'ab'", "'abcdef'", "''"]),
    ("char1", '"char"', ["'a'", "'\\'", "''"]),
    ("name", "name", ["'pg_class'", "'Mixed Case'"]),
    (
        "bytea",
        "bytea",
        [
            "''::bytea",
            "'\\x00'::bytea",
            "'\\x00ff27225c0d0a'::bytea",
            "(SELECT string_agg(set_byte('\\x00'::bytea, 0, g), ''::bytea) FROM generate_series(0, 255) g)",
            "decode(repeat('deadbeef', 300000), 'hex')",
        ],
    ),
    ("bool", "boolean", ["true", "false"]),
    (
        "date",
        "date",
        [
            "'2026-02-28'",
            "'0044-03-15 BC'",
            "'4713-01-01 BC'",
            "'5874897-12-31'",
            "'infinity'",
            "'-infinity'",
        ],
    ),
    ("time", "time(6)", ["'00:00'", "'23:59:59.999999'", "'24:00:00'"]),
    ("timetz", "timetz", ["'00:00:00+15:59'", "'12:34:56.789-15:59'", "'24:00:00+00'"]),
    (
        "timestamp",
        "timestamp",
        [
            "'2026-11-25 13:14:15.123456'",
            "'294276-12-31 23:59:59.999999'",
            "'4713-01-01 00:00 BC'",
            "'infinity'",
        ],
    ),
    (
        "timestamptz",
        "timestamptz",
        [
            "'2026-03-29 01:30:00+00'",
            "'1900-01-01 00:00:00+00'",
            "'2026-11-25 23:59:59.999999+13:45'",
            "'-infinity'",
            "'1883-11-18 12:00:00 America/New_York'",
        ],
    ),
    (
        "interval",
        "interval",
        [
            "'1 year 2 mons 3 days 04:05:06.789'",
            "'-1 day +00:00:01'",
            "'1 day -00:00:01'",
            "'-178000000 years'",
            "'0.000001 sec'",
            "'P1Y2M3DT4H5M6S'",
            "'-1 mons -1 days -1 sec'",
        ],
    ),
    (
        "uuid",
        "uuid",
        ["'00000000-0000-0000-0000-000000000000'", "'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11'"],
    ),
    (
        "json",
        "json",
        ['\'{"b": 1,   "a": [1, 2.50, null], "a": "dup"}\'', "'\"\\u0000 escaped nul\"'", "'[]'"],
    ),
    (
        "jsonb",
        "jsonb",
        ['\'{"k": "it\'\'s", "n": 1.50, "u": "\\u00e9", "z": {"x": [true, null]}}\'', "'null'"],
    ),
    ("xml", "xml", ["'<a x=\"1\">text &amp; more</a>'", "'just text <b/> more'", "''"]),
    ("inet", "inet", ["'10.0.0.1'", "'10.0.0.1/8'", "'::ffff:1.2.3.4/128'", "'2001:db8::/32'"]),
    ("cidr", "cidr", ["'10.0.0.0/8'", "'2001:db8::/32'"]),
    ("macaddr", "macaddr", ["'08:00:2b:01:02:03'"]),
    ("macaddr8", "macaddr8", ["'08:00:2b:01:02:03:04:05'"]),
    ("bit", "bit(5)", ["B'10101'", "B'00000'"]),
    ("varbit", "bit varying(10)", ["B''", "B'1'", "B'1111111111'"]),
    ("tsvector", "tsvector", ["'a fat:2 cat''s:3A sat:4B'", "''"]),
    ("tsquery", "tsquery", ["'fat & (rat | cat:*)'", "'!x <-> y'"]),
    ("point", "point", ["'(1.5,-2)'", "'(NaN,Infinity)'"]),
    ("geometry", "polygon", ["'((0,0),(1,1),(1,0))'"]),
    ("circle", "circle", ["'<(0,0),1.5>'"]),
    ("box", "box", ["'(1,1),(0,0)'"]),
    ("path", "path", ["'[(0,0),(1,1)]'", "'((0,0),(1,1),(2,0))'"]),
    ("int4range", "int4range", ["'[1,5)'", "'empty'", "'(,)'", "'[3,]'"]),
    ("numrange", "numrange", ["'[1.5,2.50]'", "'(,0)'"]),
    ("tstzrange", "tstzrange", ["'[2026-01-01 00:00+00,infinity)'"]),
    (
        "int_array",
        "integer[]",
        ["'{}'", "'{1,NULL,3}'", "'{{1,2},{3,4}}'", "'[0:1]={7,8}'", "ARRAY[]::int[]"],
    ),
    (
        "text_array",
        "text[]",
        ['\'{"a,b","c\\"d","e\\\\f","{}","NULL",NULL,""}\'', "ARRAY[E'new\\nline', '']"],
    ),
    ("jsonb_array", "jsonb[]", ["ARRAY['{\"a\": 1}'::jsonb, 'null']"]),
    ("hstore", "public.hstore", ['\'a=>1, "b c"=>NULL, "q\\""=>"x"\'', "''"]),
    ("citext", "public.citext", ["'MiXeD'"]),
    ("oid", "oid", ["0", "4294967295"]),
    ("pg_lsn", "pg_lsn", ["'16/B374D848'"]),
    ("enum", "mood", ["'ok'", "'it''s \"odd\"'", "'ünïcode'"]),
    ("enum_array", "mood[]", ["'{ok,\"it''s \\\"odd\\\"\"}'"]),
    ("composite", "pair", ["ROW(1, 'x''y')::pair", "ROW(NULL, NULL)::pair", "ROW(2, '(,)')::pair"]),
    ("composite_array", "pair[]", ["ARRAY[ROW(1, 'a')::pair, ROW(NULL, 'b, \"c\"')::pair]"]),
    ("domain", "nonempty", ["'ok'", "'with space'"]),
    ("domain_array", "nonempty[]", ["ARRAY['a'::nonempty]"]),
]
if rt.DSN and rt.server_version() >= 140000:
    # Multiranges and numeric infinities came with PostgreSQL 14.
    TYPES.append(("multirange", "int4multirange", ["'{[1,3), [5,7)}'", "'{}'"]))
    next(v for name, _, v in TYPES if name == "numeric").extend(["'Infinity'", "'-Infinity'"])

TYPES_DDL = """
CREATE TYPE mood AS ENUM ('ok', 'it''s "odd"', 'ünïcode');
CREATE TYPE pair AS (n int, s text);
CREATE DOMAIN nonempty AS text CHECK (VALUE <> '');
CREATE TABLE anchor (id int PRIMARY KEY);
""" + "\n".join(
    f"CREATE TABLE t_{name} (id int PRIMARY KEY, anchor_id int NOT NULL REFERENCES anchor, v {typ});"
    for name, typ, _ in TYPES
)


def types_data() -> str:
    lines = ["INSERT INTO anchor VALUES (1), (2);"]
    for name, _, values in TYPES:
        rows = [f"({i}, 1, {v})" for i, v in enumerate(values, start=1)]
        rows.append(f"({len(values) + 1}, 1, NULL)")
        rows.append(f"({len(values) + 2}, 2, NULL)")  # belongs to the other anchor
        lines.append(f"INSERT INTO t_{name} VALUES {', '.join(rows)};")
    return "\n".join(lines)


@pytest.mark.parametrize(("rows_per_insert", "crlf"), [(1, False), (50, False), (1, True)])
def test_every_data_type_round_trips(env, rows_per_insert, crlf):
    env.setup(TYPES_DDL, types_data())
    result, _ = env.roundtrip(
        "rt.anchor",
        "id = 1",
        rows_per_insert=rows_per_insert,
        on_conflict_do_nothing=True,
        replay_twice=True,
        crlf=crlf,
    )
    assert result.total_rows == 1 + sum(len(v) + 1 for _, _, v in TYPES)


def test_hostile_session_settings_on_both_sides(env):
    """Source and target sessions with every output-affecting setting changed."""
    env.setup(TYPES_DDL, types_data())
    env.roundtrip(
        "rt.anchor",
        "id = 1",
        source_sql=(
            "SET DateStyle = 'SQL, DMY'; SET IntervalStyle = 'sql_standard'; "
            "SET extra_float_digits = -15; SET bytea_output = 'escape'; "
            "SET TimeZone = 'Pacific/Chatham'; SET standard_conforming_strings = off"
        ),
        session_sql=(
            "SET DateStyle = 'German, MDY'; SET IntervalStyle = 'iso_8601'; "
            "SET TimeZone = 'America/St_Johns'; SET standard_conforming_strings = off; "
            "SET xmloption = document; SET search_path = pg_catalog"
        ),
    )


# -- foreign keys on every kind of key ------------------------------------

# (suffix, key type, parent keys, child references as (key expr, also-referenced parent))
KEYS: list[tuple[str, str, list[str], list[str]]] = [
    ("int8", "bigint", ["1", "2", "3"], ["1", "3"]),
    ("text", "text", ["'a'", "E'new\\nline'", "'it''s'", "'🙂'"], ["'a'", "E'new\\nline'", "'🙂'"]),
    ("varchar", "varchar(5)", ["'abcde'", "'x'"], ["'abcde'", "'x'"]),
    ("bpchar", "char(6)", ["'ab'", "'abcdef'"], ["'ab'", "'abcdef'"]),
    ("bit", "bit(5)", ["B'10100'", "B'00001'"], ["B'10100'", "B'00001'"]),
    ("varbit", "varbit", ["B'1'", "B'1010'"], ["B'1'", "B'1010'"]),
    ("numeric", "numeric(10,2)", ["1.50", "2.00", "-0.01"], ["1.5", "2", "-0.010"]),
    ("float8", "float8", ["'NaN'", "'1e-300'", "'Infinity'"], ["'NaN'", "'1e-300'"]),
    ("interval", "interval", ["'1 day'", "'1 mon'"], ["'24 hours'", "'30 days'"]),
    (
        "timestamptz",
        "timestamptz",
        ["'2026-01-01 00:00:00.000001+00'"],
        ["'2026-01-01 05:00:00.000001+05'"],
    ),
    ("date", "date", ["'2026-01-01'", "'infinity'"], ["'2026-01-01'", "'infinity'"]),
    (
        "uuid",
        "uuid",
        ["'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'"],
        ["'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11'"],
    ),
    ("inet", "inet", ["'10.0.0.1'", "'::1'"], ["'10.0.0.1/32'", "'::1'"]),
    ("bytea", "bytea", ["'\\x00ff'::bytea", "''::bytea"], ["'\\x00ff'::bytea", "''::bytea"]),
    ("bool", "boolean", ["true", "false"], ["true"]),
    ("enum", "mood", ["'ok'", "'it''s \"odd\"'"], ["'ok'", "'it''s \"odd\"'"]),
    ("citext", "public.citext", ["'Alpha'", "'beta'"], ["'ALPHA'", "'Beta'"]),
    ("int_array", "integer[]", ["'{1,2}'", "'{}'"], ["'{1,2}'", "'{}'"]),
    ("int4range", "int4range", ["'[1,5)'", "'empty'"], ["'[1,4]'", "'empty'"]),
    ("composite", "pair", ["ROW(1, 'a')::pair", "ROW(2, NULL)::pair"], ["ROW(1, 'a')::pair"]),
    ("oid", "oid", ["1", "4294967295"], ["1", "4294967295"]),
    ("char1", '"char"', ["'a'", "'b'"], ["'a'", "'b'"]),
    ("money", "money", ["'1.00'", "'-2.50'"], ["'1.00'", "'-2.50'"]),
]

KEYS_DDL = """
CREATE TYPE mood AS ENUM ('ok', 'it''s "odd"');
CREATE TYPE pair AS (n int, s text);
CREATE TABLE anchor (id int PRIMARY KEY);
""" + "\n".join(
    f"""CREATE TABLE p_{n} (k {t} PRIMARY KEY, anchor_id int REFERENCES anchor);
CREATE TABLE c_{n} (id int PRIMARY KEY, k {t} REFERENCES p_{n}, other {t} REFERENCES p_{n});"""
    for n, t, _, _ in KEYS
)


def keys_data() -> str:
    lines = ["INSERT INTO anchor VALUES (1), (2);"]
    for n, _, parents, refs in KEYS:
        # The first parent belongs to anchor 1; the others to anchor 2.
        prow = [f"({k}, {1 if i == 0 else 2})" for i, k in enumerate(parents)]
        lines.append(f"INSERT INTO p_{n} VALUES {', '.join(prow)};")
        # Child rows of anchor-1's parent, each also referencing another parent.
        crow = [f"({i}, {refs[0]}, {r})" for i, r in enumerate(refs, start=1)]
        crow.append(f"({len(refs) + 1}, {refs[-1]}, NULL)")  # child of an anchor-2 parent
        lines.append(f"INSERT INTO c_{n} VALUES {', '.join(crow)};")
    return "\n".join(lines)


def test_foreign_keys_on_every_key_type(env):
    """Lookups in both directions must follow PostgreSQL equality, not text."""
    env.setup(KEYS_DDL, keys_data())
    result, _ = env.roundtrip("rt.anchor", "id = 1")
    counts = {t.name: n for t, n in result.counts}
    for n, _, _, refs in KEYS:
        # The oracle check above is exact; this guards against a vacuous pass where
        # a key type's lookups found nothing in either direction.
        assert counts.get(f"c_{n}", 0) >= 1, n
        if len(refs) > 1:
            assert counts.get(f"p_{n}", 0) >= 2, n  # referenced parents were pulled in


# -- identifiers -------------------------------------------------------------


def test_hostile_identifiers(env):
    ddl = """
    CREATE TABLE "select" ("from" int PRIMARY KEY, "Mixed Case" text);
    CREATE TABLE "we""ird" (
        "a b" int, "c;d" text, "%s" int, "{}" text, "表" int,
        PRIMARY KEY ("a b", "c;d"),
        FOREIGN KEY ("%s") REFERENCES "select"
    );
    CREATE TABLE "new
line
CREATE TABLE rt.pwned (x int);
--" ("id" int PRIMARY KEY, "a b" int, "c;d" text,
        FOREIGN KEY ("c;d", "a b") REFERENCES "we""ird" ("c;d", "a b"));
    """
    data = """
    INSERT INTO "select" VALUES (1, 'x'), (2, 'y');
    INSERT INTO "we""ird" VALUES (10, 'k;1', 1, '{}', 7), (11, 'k;2', 2, NULL, NULL);
    INSERT INTO "new
line
CREATE TABLE rt.pwned (x int);
--" VALUES (100, 10, 'k;1'), (101, 11, 'k;2');
    """
    env.setup(ddl, data)
    env.roundtrip('rt."select"', '"from" = 1', rows_per_insert=3, crlf=True)
    exists = env.dst.execute("SELECT to_regclass('rt.pwned')").fetchone()[0]
    assert exists is None, "a table name injected SQL into the dump"


def test_output_schema_with_hostile_name(env):
    env.setup("CREATE TABLE t (id int PRIMARY KEY);", "INSERT INTO t VALUES (1);")
    target = 'Out "Schema"\n;--'
    q = '"Out ""Schema""\n;--"'
    env.dst.execute(f"DROP SCHEMA IF EXISTS {q} CASCADE; CREATE SCHEMA {q}")
    env.dst.execute(f"CREATE TABLE {q}.t (id int PRIMARY KEY)")
    try:
        _, path = env.dump("SELECT * FROM rt.t", output_schemas=[target])
        env.replay(path)
        assert env.dst.execute(f"SELECT id FROM {q}.t").fetchall() == [(1,)]
    finally:
        env.dst.execute(f"DROP SCHEMA {q} CASCADE")


# -- structural edge cases -------------------------------------------------------

STRUCT_DDL = (
    """
CREATE TABLE root (id int PRIMARY KEY, label text);
-- foreign key to a unique, non-primary-key column
CREATE TABLE acct (id int PRIMARY KEY, code text NOT NULL UNIQUE, root_id int REFERENCES root);
CREATE TABLE acct_ref (id int PRIMARY KEY, acct_code text REFERENCES acct (code));
-- composite key referenced in a different column order
CREATE TABLE comp (a int, b text, root_id int REFERENCES root, PRIMARY KEY (a, b));
CREATE TABLE comp_ref (id int PRIMARY KEY, bb text, aa int, FOREIGN KEY (bb, aa) REFERENCES comp (b, a));
-- MATCH FULL
CREATE TABLE full_ref (id int PRIMARY KEY, a int, b text,
    FOREIGN KEY (a, b) REFERENCES comp MATCH FULL, root_id int REFERENCES root);
-- int4 referencing int8, two foreign keys to one table
CREATE TABLE big (id bigint PRIMARY KEY);
CREATE TABLE two_refs (id int PRIMARY KEY, root_id int REFERENCES root,
    made_by int REFERENCES big, changed_by int REFERENCES big);
-- identity and generated columns
CREATE TABLE ident (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, root_id int REFERENCES root,
    by_default int GENERATED BY DEFAULT AS IDENTITY,
    doubled int GENERATED ALWAYS AS (id * 2) STORED);
-- no key at all, with duplicate rows
CREATE TABLE nokey (root_id int REFERENCES root, msg text);
-- partitioned, referenced and referencing
CREATE TABLE part (id int, at date, root_id int REFERENCES root, PRIMARY KEY (id, at))
    PARTITION BY RANGE (at);
CREATE TABLE part_a PARTITION OF part FOR VALUES FROM ('2000-01-01') TO ('2025-01-01');
CREATE TABLE part_b PARTITION OF part FOR VALUES FROM ('2025-01-01') TO ('2100-01-01');
CREATE TABLE part_ref (id int PRIMARY KEY, pid int, pat date, FOREIGN KEY (pid, pat) REFERENCES part);
-- inheritance: rows of the child table must not be exported as parent rows
CREATE TABLE inh_parent (id int PRIMARY KEY, root_id int REFERENCES root);
CREATE TABLE inh_child (extra text) INHERITS (inh_parent);
-- self-reference, and a two-table cycle through a nullable column
CREATE TABLE tree (id int PRIMARY KEY, parent_id int REFERENCES tree, root_id int REFERENCES root);
CREATE TABLE team (id int PRIMARY KEY, root_id int REFERENCES root, captain_id int);
CREATE TABLE member (id int PRIMARY KEY, team_id int NOT NULL REFERENCES team);
ALTER TABLE team ADD FOREIGN KEY (captain_id) REFERENCES member;
-- many columns
CREATE TABLE wide (id int PRIMARY KEY, root_id int REFERENCES root,
"""
    + ",\n".join(f"    c{i} int" for i in range(200))
    + """);
"""
)

STRUCT_DATA = """
INSERT INTO root VALUES (1, 'one'), (2, 'two');
INSERT INTO acct VALUES (1, 'A-1', 1), (2, 'A-2', 2), (3, 'A-3', NULL);
INSERT INTO acct_ref VALUES (1, 'A-1'), (2, 'A-3'), (3, 'A-2');
INSERT INTO comp VALUES (1, 'x', 1), (2, 'y', 2), (3, 'z', NULL);
INSERT INTO comp_ref VALUES (1, 'x', 1), (2, 'z', 3), (3, 'y', 2);
INSERT INTO full_ref VALUES (1, 3, 'z', 1), (2, NULL, NULL, 1), (3, 2, 'y', 2);
INSERT INTO big VALUES (1), (2), (3);
INSERT INTO two_refs VALUES (1, 1, 1, 2), (2, 2, 3, 3), (3, 1, 2, NULL);
INSERT INTO ident (root_id) VALUES (1), (2), (1);
INSERT INTO nokey VALUES (1, 'dup'), (1, 'dup'), (2, 'other'), (NULL, 'orphan');
INSERT INTO part VALUES (1, '2001-01-01', 1), (2, '2030-01-01', 1), (3, '2030-01-01', 2);
INSERT INTO part_ref VALUES (1, 1, '2001-01-01'), (2, 3, '2030-01-01');
INSERT INTO inh_parent VALUES (1, 1);
INSERT INTO inh_child VALUES (2, 1, 'child row');
INSERT INTO tree SELECT g, NULLIF(g / 3, 0), CASE WHEN g = 1 THEN 1 END FROM generate_series(1, 400) g;
INSERT INTO team VALUES (1, 1, NULL), (2, 2, NULL);
INSERT INTO member VALUES (5, 1), (6, 1), (7, 2);
UPDATE team SET captain_id = 5 WHERE id = 1;
UPDATE team SET captain_id = 7 WHERE id = 2;
INSERT INTO wide (id, root_id, c0, c199) VALUES (1, 1, 0, 199), (2, 2, NULL, NULL);
"""


def _struct_ddl() -> str:
    """STRUCT_DDL, without the generated column before PostgreSQL 12."""
    if rt.server_version() >= 120000:
        return STRUCT_DDL
    # PostgreSQL 11 also can't reference a partitioned table.
    return STRUCT_DDL.replace(",\n    doubled int GENERATED ALWAYS AS (id * 2) STORED", "").replace(
        ", FOREIGN KEY (pid, pat) REFERENCES part)", ")"
    )


@pytest.mark.parametrize(
    "opts",
    [
        {},
        {"rows_per_insert": 7, "on_conflict_do_nothing": True, "transaction": True},
        {"batch_size": 1, "fetch_size": 1},
        {"follow_children": False},
    ],
    ids=["default", "multi-row+conflict+tx", "tiny-batches", "no-children"],
)
def test_structural_edge_cases(env, opts):
    env.setup(_struct_ddl(), STRUCT_DATA)
    env.roundtrip("rt.root", "id = 1", **opts)


def test_structural_edge_cases_all_rows(env):
    env.setup(_struct_ddl(), STRUCT_DATA)
    env.roundtrip("rt.root", "true", rows_per_insert=3)


def test_no_parents_into_target_that_has_them(env):
    env.setup(
        "CREATE TABLE lookup (id int PRIMARY KEY, name text);"
        "CREATE TABLE root (id int PRIMARY KEY, lookup_id int REFERENCES lookup);"
        "CREATE TABLE child (id int PRIMARY KEY, root_id int REFERENCES root, "
        "lookup_id int NOT NULL REFERENCES lookup);",
        "INSERT INTO lookup VALUES (1, 'a'), (2, 'b');"
        "INSERT INTO root VALUES (1, 1), (2, 2);"
        "INSERT INTO child VALUES (1, 1, 2), (2, 2, 1), (3, 1, 1);",
    )
    env.dst.execute("INSERT INTO rt.lookup VALUES (1, 'a'), (2, 'b')")
    expected = env.expected("rt.root", "id = 1", parents=False)
    expected["rt.lookup"] = ["(1,a)", "(2,b)"]
    _, path = env.dump("SELECT * FROM rt.root WHERE id = 1", follow_parents=False)
    assert "-- Data for rt.lookup" not in path.read_text(encoding="utf-8")
    env.replay(path)
    env.assert_target(expected)


def test_set_sequences_after_import(env):
    env.setup(
        "CREATE TABLE s (id serial PRIMARY KEY, x int); "
        "CREATE TABLE i (id bigint GENERATED ALWAYS AS IDENTITY (START WITH 1000) PRIMARY KEY);",
        "INSERT INTO s (x) SELECT g FROM generate_series(1, 50) g; "
        "INSERT INTO i DEFAULT VALUES; INSERT INTO i DEFAULT VALUES;",
    )
    _, p1 = env.dump("SELECT * FROM rt.s", name="s.sql", set_sequences=True)
    _, p2 = env.dump("SELECT * FROM rt.i", name="i.sql", set_sequences=True)
    env.replay(p1)
    env.replay(p2)
    assert env.dst.execute("INSERT INTO rt.s (x) VALUES (0) RETURNING id").fetchone()[0] == 51
    assert env.dst.execute("INSERT INTO rt.i DEFAULT VALUES RETURNING id").fetchone()[0] == 1002
    # A target sequence that is already further along is never moved back.
    env.dst.execute("SELECT setval('rt.s_id_seq', 5000)")
    _, p3 = env.dump(
        "SELECT * FROM rt.s WHERE id = 1",
        name="s1.sql",
        set_sequences=True,
        on_conflict_do_nothing=True,
    )
    env.replay(p3)
    assert env.dst.execute("SELECT nextval('rt.s_id_seq')").fetchone()[0] == 5001


def test_deep_self_reference_chain(env):
    env.setup(
        "CREATE TABLE node (id int PRIMARY KEY, parent_id int REFERENCES node);",
        # ids descend as depth grows, so key order is the reverse of insert order
        "INSERT INTO node SELECT g, CASE WHEN g < 3000 THEN g + 1 END "
        "FROM generate_series(1, 3000) g;",
    )
    env.roundtrip("rt.node", "id = 3000")


def test_not_null_cycle_needs_deferrable_constraints(env):
    env.setup(
        "CREATE TABLE a (id int PRIMARY KEY, b_id int NOT NULL);"
        "CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL REFERENCES a "
        "DEFERRABLE INITIALLY IMMEDIATE);"
        "ALTER TABLE a ADD FOREIGN KEY (b_id) REFERENCES b DEFERRABLE INITIALLY IMMEDIATE;",
        "BEGIN; SET CONSTRAINTS ALL DEFERRED; INSERT INTO a VALUES (1, 1); "
        "INSERT INTO b VALUES (1, 1); COMMIT;",
    )
    result, _ = env.roundtrip("rt.a", "true", transaction=True)
    assert result.plan.warnings


def test_snapshot_ignores_concurrent_changes(env, monkeypatch):
    """Rows changed while the dump runs must not leak in: one consistent snapshot."""
    import psycopg

    import rt as rtmod
    from intellidump import source

    env.setup(
        "CREATE TABLE p (id int PRIMARY KEY, v text); "
        "CREATE TABLE c (id int PRIMARY KEY, p_id int NOT NULL REFERENCES p);",
        "INSERT INTO p VALUES (1, 'before'); INSERT INTO c VALUES (1, 1);",
    )
    expected = env.expected("rt.p", "id = 1")
    real_fetch = source.PgRowSource.fetch
    state = {"done": False}

    def meddling_fetch(self, table, cols, values, **kwargs):
        if not state["done"]:
            state["done"] = True
            with psycopg.connect(rtmod.DSN, autocommit=True) as other:
                other.execute("UPDATE rt.p SET v = 'after'; INSERT INTO rt.c VALUES (2, 1);")
        return real_fetch(self, table, cols, values, **kwargs)

    monkeypatch.setattr(source.PgRowSource, "fetch", meddling_fetch)
    _, path = env.dump("SELECT * FROM rt.p WHERE id = 1")
    assert state["done"]
    env.replay(path)
    env.assert_target(expected)


# -- failure handling ------------------------------------------------------------


def test_failed_dump_keeps_old_file_and_cleans_up(env):
    from intellidump.errors import IntellidumpError

    env.setup("CREATE TABLE t (id int PRIMARY KEY);", "INSERT INTO t VALUES (1);")
    out = env.tmp / "dump.sql"
    out.write_text("previous good dump", encoding="utf-8")
    with pytest.raises(IntellidumpError):
        env.dump("SELECT * FROM rt.t", table="rt.nope")
    assert out.read_text(encoding="utf-8") == "previous good dump"
    assert [p.name for p in env.tmp.iterdir() if p.name != "work"] == ["dump.sql"]


def test_query_cannot_write(env):
    import psycopg

    env.setup(
        "CREATE TABLE t (id int PRIMARY KEY); CREATE SEQUENCE s;"
        "CREATE FUNCTION bump() RETURNS int LANGUAGE sql AS 'INSERT INTO rt.t VALUES (99) RETURNING id';",
        "INSERT INTO t VALUES (1);",
    )
    for query in (
        "SELECT * FROM rt.t WHERE id = nextval('rt.s')",
        "SELECT * FROM rt.t WHERE id = rt.bump()",
        "SELECT * FROM rt.t; DELETE FROM rt.t",
        "SELECT * FROM rt.t FOR UPDATE",
    ):
        with pytest.raises(Exception) as exc:
            env.dump(query)
        assert isinstance(exc.value, psycopg.Error) or "single SELECT" in str(exc.value)
    assert env.src.execute("SELECT array_agg(id) FROM rt.t").fetchone()[0] == [1]


def test_row_level_security_fails_loudly(env):
    import psycopg

    env.setup(
        "CREATE TABLE o (id int PRIMARY KEY); "
        "CREATE TABLE s (id int PRIMARY KEY, o_id int REFERENCES o, hidden bool);"
        "ALTER TABLE s ENABLE ROW LEVEL SECURITY; ALTER TABLE s FORCE ROW LEVEL SECURITY;"
        "CREATE POLICY p ON s USING (NOT hidden);",
        "INSERT INTO o VALUES (1); INSERT INTO s VALUES (1, 1, false), (2, 1, true);",
    )
    env.src.execute("DROP ROLE IF EXISTS rt_reader; CREATE ROLE rt_reader")
    env.src.execute(
        "GRANT USAGE ON SCHEMA rt TO rt_reader; GRANT SELECT ON ALL TABLES IN SCHEMA rt TO rt_reader"
    )
    try:
        env.src.execute("SET ROLE rt_reader")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            env.dump("SELECT * FROM rt.o")
    finally:
        env.src.execute("RESET ROLE")
        env.src.execute("DROP OWNED BY rt_reader; DROP ROLE rt_reader")


def test_union_and_cast_queries_with_table(env):
    env.setup(
        "CREATE TABLE t (id int PRIMARY KEY); CREATE TABLE c (id int PRIMARY KEY, t_id int REFERENCES t);",
        "INSERT INTO t VALUES (1), (2), (3); INSERT INTO c VALUES (1, 1), (2, 3);",
    )
    env.roundtrip(
        "rt.t",
        "id IN (1, 3)",
        query="SELECT id FROM rt.t WHERE id = 1 UNION SELECT id::bigint FROM rt.t WHERE id = 3",
        table="rt.t",
    )


@pytest.mark.parametrize(
    ("key_type", "ref_type", "rows"),
    [
        # (key, reference) pairs: the reference is equal to its parent's key in
        # PostgreSQL but has different text, and sorts before it.
        ("public.citext", "public.citext", "('b', NULL), ('a', 'B')"),
        ("interval", "interval", "('2 days', NULL), ('1 day', '48 hours')"),
        ("numeric(10,2)", "numeric", "(2.00, NULL), (1.00, 2)"),
    ],
    ids=["citext", "interval", "numeric-scale"],
)
def test_cycle_ordering_uses_postgres_equality(env, key_type, ref_type, rows):
    env.setup(
        f"CREATE TABLE n (k {key_type} PRIMARY KEY, parent {ref_type} REFERENCES n);",
        f"INSERT INTO n VALUES {rows};",
    )
    env.roundtrip("rt.n", "true")


def test_cli_with_only_environment_variables(env, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    from intellidump.cli import main

    env.setup(
        "CREATE TABLE p (id int PRIMARY KEY); CREATE TABLE c (id int PRIMARY KEY, p_id int REFERENCES p);",
        "INSERT INTO p VALUES (1), (2); INSERT INTO c VALUES (1, 1), (2, 2);",
    )
    parts = conninfo_to_dict(env.src.info.dsn)
    for var, key in [
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGUSER", "user"),
        ("PGDATABASE", "dbname"),
    ]:
        monkeypatch.setenv(var, str(parts[key]))
    monkeypatch.setenv("PGPASSWORD", env.src.info.password or "")
    out = env.tmp / "cli.sql"
    code = main(
        [
            "-f",
            str(out),
            "-n",
            "rt",
            "--work-dir",
            str(env.tmp / "work"),
            "-q",
            "SELECT * FROM p WHERE id = 1",
        ]
    )
    assert code == 0
    env.replay(out)
    env.assert_target(env.expected("rt.p", "id = 1"))


def test_crash_mid_traversal_leaves_nothing_behind(env, monkeypatch):
    from intellidump import source

    env.setup(
        "CREATE TABLE p (id int PRIMARY KEY); CREATE TABLE c (id int PRIMARY KEY, p_id int REFERENCES p);",
        "INSERT INTO p VALUES (1); INSERT INTO c VALUES (1, 1);",
    )

    def boom(self, table, cols, values, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(source.PgRowSource, "fetch", boom)
    with pytest.raises(RuntimeError):
        env.dump("SELECT * FROM rt.p")
    assert not (env.tmp / "dump.sql").exists()
    assert not list((env.tmp / "work").iterdir())
    # The caller's connection is usable and back in autocommit mode.
    assert env.src.autocommit and env.src.execute("SELECT 1").fetchone() == (1,)
