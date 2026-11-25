"""End-to-end tests against a real PostgreSQL server.

Set ``INTELLIDUMP_TEST_DSN`` to a conninfo/URI for a database you don't mind
the tests creating and dropping the ``idump_src`` and ``idump_other`` schemas
in, e.g.::

    INTELLIDUMP_TEST_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from intellidump.cli import main  # noqa: E402
from intellidump.dump import DumpOptions, run_dump_on  # noqa: E402
from intellidump.errors import IntellidumpError  # noqa: E402

DSN = os.environ.get("INTELLIDUMP_TEST_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="INTELLIDUMP_TEST_DSN is not set"),
]

SCHEMA_SQL = '''
DROP SCHEMA IF EXISTS idump_other CASCADE;
DROP SCHEMA IF EXISTS idump_src CASCADE;
CREATE SCHEMA idump_src;
CREATE SCHEMA idump_other;
SET search_path = idump_src;

CREATE TABLE model (id serial PRIMARY KEY, name text NOT NULL);
CREATE TABLE app_user (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email text NOT NULL UNIQUE
);
CREATE TABLE prediction (
    id serial PRIMARY KEY,
    model_id int NOT NULL REFERENCES model,
    created timestamptz NOT NULL,
    payload jsonb,
    tags text[],
    score numeric,
    ratio float8,
    window_ tstzrange
);
CREATE TABLE entity (
    id int PRIMARY KEY,
    prediction_id int NOT NULL REFERENCES prediction,
    parent_id int REFERENCES entity,
    label text,
    upper_label text GENERATED ALWAYS AS (upper(label)) STORED
);
CREATE TABLE action (
    id int PRIMARY KEY,
    entity_id int NOT NULL REFERENCES entity,
    user_id bigint REFERENCES app_user,
    note text,
    data bytea
);
-- composite key + composite foreign key
CREATE TABLE run (prediction_id int REFERENCES prediction, seq int, PRIMARY KEY (prediction_id, seq));
CREATE TABLE run_step (
    id serial PRIMARY KEY, prediction_id int NOT NULL, seq int NOT NULL, info text,
    FOREIGN KEY (prediction_id, seq) REFERENCES run
);
-- two tables referencing each other
CREATE TABLE team (id int PRIMARY KEY, prediction_id int REFERENCES prediction, captain_id int);
CREATE TABLE member (id int PRIMARY KEY, team_id int NOT NULL REFERENCES team);
ALTER TABLE team ADD FOREIGN KEY (captain_id) REFERENCES member;
-- no primary key at all
CREATE TABLE log_entry (prediction_id int REFERENCES prediction, msg text);
-- partitioned
CREATE TABLE event (
    id int, at date, prediction_id int REFERENCES prediction, PRIMARY KEY (id, at)
) PARTITION BY RANGE (at);
CREATE TABLE event_2026 PARTITION OF event FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE event_old PARTITION OF event FOR VALUES FROM ('2000-01-01') TO ('2026-01-01');
-- weird identifiers
CREATE TABLE "Odd ""Name""" (id int PRIMARY KEY, "Prediction Id" int REFERENCES prediction);
-- another schema pointing in (not exported)
CREATE TABLE idump_other.ext_ref (id int PRIMARY KEY, prediction_id int REFERENCES idump_src.prediction);

INSERT INTO model (name) VALUES ('m1'), ('m2'), ('unused');
INSERT INTO app_user (email) VALUES ('a@x'), ('b@x'), ('c@x');
INSERT INTO prediction (model_id, created, payload, tags, score, ratio, window_) VALUES
    (1, now() - interval '1 day', '{"k": "it''s"}', '{a,"b c"}', 1.50, 'NaN', '[2026-01-01,2026-02-01)'),
    (1, now() - interval '30 days', NULL, NULL, NULL, NULL, NULL),
    (2, now() - interval '2 days', '[1, 2]', '{}', -3, 1e-300, NULL);
INSERT INTO entity (id, prediction_id, parent_id, label) VALUES
    (12, 1, NULL, 'root'), (10, 1, 12, 'child'), (13, 1, 10, 'grandchild'),
    (11, 1, NULL, E'tab\\there'), (20, 2, NULL, 'old'), (30, 3, NULL, 'p3');
INSERT INTO action (id, entity_id, user_id, note, data) VALUES
    (100, 10, 1, 'x', '\\x00ff'), (101, 13, 2, NULL, NULL), (102, 20, 3, 'old', NULL),
    (103, 30, NULL, 'quote''s', NULL);
INSERT INTO run VALUES (1, 1), (1, 2), (2, 1);
INSERT INTO run_step (prediction_id, seq, info) VALUES (1, 2, 's'), (2, 1, 'old');
INSERT INTO team VALUES (1, 1, NULL), (2, 2, NULL);
INSERT INTO member VALUES (5, 1), (6, 1), (7, 2);
UPDATE team SET captain_id = 5 WHERE id = 1;
UPDATE team SET captain_id = 7 WHERE id = 2;
INSERT INTO log_entry VALUES (1, 'one'), (1, 'one'), (2, 'two');
INSERT INTO event VALUES (1, '2026-05-01', 1), (2, '2020-01-01', 1), (3, '2026-05-01', 2);
INSERT INTO "Odd ""Name""" VALUES (1, 3), (2, 2);
INSERT INTO idump_other.ext_ref VALUES (1, 1);
'''

QUERY = "SELECT * FROM prediction WHERE created > CURRENT_TIMESTAMP - INTERVAL '7 days';"

TABLES = [
    "model",
    "app_user",
    "prediction",
    "entity",
    "action",
    "run",
    "run_step",
    "team",
    "member",
    "log_entry",
    "event",
    '"Odd ""Name"""',
]


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(SCHEMA_SQL)
        c.execute("RESET search_path")
        yield c
        c.execute("DROP SCHEMA IF EXISTS idump_other CASCADE")
        c.execute("DROP SCHEMA IF EXISTS idump_src CASCADE")


def snapshot(c) -> dict[str, list[tuple]]:
    out = {}
    for t in TABLES:
        rows = c.execute(f"SELECT t::text FROM idump_src.{t} t ORDER BY 1").fetchall()
        out[t] = [r[0] for r in rows]
    return out


def expected_subset(c) -> dict[str, list[str]]:
    """What the dump of QUERY should contain, computed independently."""
    where = {
        "model": "id IN (1, 2)",
        "app_user": "id IN (1, 2)",
        "prediction": "id IN (1, 3)",
        "entity": "prediction_id IN (1, 3)",
        "action": "id IN (100, 101, 103)",
        "run": "prediction_id = 1",
        "run_step": "prediction_id = 1",
        "team": "id = 1",
        "member": "team_id = 1",
        "log_entry": "prediction_id = 1",
        "event": "prediction_id = 1",
        '"Odd ""Name"""': "id = 1",
    }
    out = {}
    for t in TABLES:
        rows = c.execute(f"SELECT t::text FROM idump_src.{t} t WHERE {where[t]} ORDER BY 1")
        out[t] = [r[0] for r in rows.fetchall()]
    return out


def wipe(c) -> None:
    c.execute(
        "TRUNCATE "
        + ", ".join(f"idump_src.{t}" for t in TABLES)
        + ", idump_other.ext_ref RESTART IDENTITY"
    )


def replay(c, path: Path) -> None:
    c.execute(path.read_text(encoding="utf-8"))
    c.execute("RESET ALL")


def test_dump_and_reimport(conn, tmp_path):
    out = tmp_path / "dump.sql"
    expected = expected_subset(conn)
    result = run_dump_on(
        conn,
        DumpOptions(
            query=QUERY,
            output=out,
            schemas=["idump_src"],
            on_conflict_do_nothing=True,
            set_sequences=True,
        ),
    )
    assert result.root.name == "prediction"
    sql = out.read_text(encoding="utf-8")
    assert "BEGIN;" not in sql
    assert "ext_ref" not in sql.split("-- Data for", 1)[1]
    assert "upper_label" not in sql  # generated column
    assert "OVERRIDING SYSTEM VALUE" in sql  # app_user identity ALWAYS

    # Parents strictly before children in the file.
    order = [t.name for t, _ in result.counts]
    assert order.index("model") < order.index("prediction") < order.index("entity")
    assert order.index("entity") < order.index("action")

    wipe(conn)
    replay(conn, out)
    assert snapshot(conn) == expected

    # Running it again is a no-op thanks to ON CONFLICT DO NOTHING...
    replay(conn, out)
    after = snapshot(conn)
    # ...except for log_entry, which has no key to conflict on.
    assert {k: v for k, v in after.items() if k != "log_entry"} == {
        k: v for k, v in expected.items() if k != "log_entry"
    }

    # Sequences (reset by the TRUNCATE) were moved past the imported ids.
    assert conn.execute("SELECT nextval('idump_src.prediction_id_seq')").fetchone()[0] == 4
    assert (
        conn.execute(
            "SELECT nextval(pg_get_serial_sequence('idump_src.app_user', 'id'))"
        ).fetchone()[0]
        == 3
    )


def test_transaction_flag_and_rows_per_insert(conn, tmp_path):
    out = tmp_path / "dump.sql"
    expected = expected_subset(conn)
    run_dump_on(
        conn,
        DumpOptions(
            query=QUERY,
            output=out,
            schemas=["idump_src"],
            transaction=True,
            rows_per_insert=100,
        ),
    )
    sql = out.read_text(encoding="utf-8")
    assert sql.count("BEGIN;") == 1 and sql.count("COMMIT;") == 1
    assert "ON CONFLICT" not in sql
    wipe(conn)
    replay(conn, out)
    assert snapshot(conn) == expected
    # Without ON CONFLICT DO NOTHING a second import fails and rolls back cleanly.
    with pytest.raises(psycopg.errors.UniqueViolation):
        replay(conn, out)
    conn.execute("ROLLBACK")
    assert snapshot(conn) == expected


def test_no_parents(conn, tmp_path):
    out = tmp_path / "dump.sql"
    result = run_dump_on(
        conn,
        DumpOptions(
            query="SELECT id FROM idump_src.entity WHERE id = 10", output=out, follow_parents=False
        ),
    )
    names = {t.name: n for t, n in result.counts}
    assert names == {"entity": 2, "action": 2}  # 10, 13 and their actions


def test_join_needs_table_flag(conn, tmp_path):
    q = (
        "SELECT p.*, m.* FROM idump_src.prediction p "
        "JOIN idump_src.model m ON m.id = p.model_id WHERE p.id = 1"
    )
    with pytest.raises(IntellidumpError, match="--table"):
        run_dump_on(conn, DumpOptions(query=q, output=tmp_path / "x.sql"))
    result = run_dump_on(
        conn,
        DumpOptions(
            query=q, output=tmp_path / "x.sql", table="idump_src.model", follow_children=False
        ),
    )
    assert [(t.name, n) for t, n in result.counts] == [("model", 1)]


def test_query_without_key_is_rejected(conn, tmp_path):
    with pytest.raises(IntellidumpError, match="primary key"):
        run_dump_on(
            conn,
            DumpOptions(
                query="SELECT created FROM idump_src.prediction", output=tmp_path / "x.sql"
            ),
        )


def test_non_select_is_rejected(conn, tmp_path):
    with pytest.raises((IntellidumpError, psycopg.Error)):
        run_dump_on(
            conn,
            DumpOptions(query="DELETE FROM idump_src.model RETURNING *", output=tmp_path / "x.sql"),
        )
    assert conn.execute("SELECT count(*) FROM idump_src.model").fetchone()[0] == 3


def test_output_schema_with_different_column_order(conn, tmp_path):
    # The target table has its columns in another order, plus an extra column
    # with a default: the explicit column list in each INSERT makes that work.
    conn.execute("""
        CREATE SCHEMA idump_dst;
        CREATE TABLE idump_dst.model (
            name text NOT NULL, added text DEFAULT 'x', id serial PRIMARY KEY
        );
    """)
    try:
        out = tmp_path / "dump.sql"
        run_dump_on(
            conn,
            DumpOptions(
                query="SELECT * FROM idump_src.model WHERE id IN (1, 2)",
                output=out,
                follow_children=False,
                output_schemas=["idump_dst"],
                set_sequences=True,
            ),
        )
        sql = out.read_text(encoding="utf-8")
        assert 'INSERT INTO "idump_dst"."model" ("id", "name")' in sql
        assert '"idump_src"' not in sql.split("SET client_min_messages", 1)[1]
        replay(conn, out)
        rows = conn.execute("SELECT id, name, added FROM idump_dst.model ORDER BY id").fetchall()
        assert rows == [(1, "m1", "x"), (2, "m2", "x")]
        # The target's own sequence was advanced; the source's was not touched.
        assert conn.execute("SELECT nextval('idump_dst.model_id_seq')").fetchone()[0] == 3
    finally:
        conn.execute("DROP SCHEMA idump_dst CASCADE")


def test_cli_end_to_end(conn, tmp_path, monkeypatch):
    info = conn.info
    monkeypatch.setenv("PGPASSWORD", info.password or "")
    out = tmp_path / "cli.sql"
    code = main(
        [
            "-h",
            info.host,
            "-p",
            str(info.port),
            "-U",
            info.user,
            "-d",
            info.dbname,
            "-n",
            "idump_src",
            "-f",
            str(out),
            "--on-conflict-do-nothing",
            "--query",
            QUERY,
        ]
    )
    assert code == 0
    assert "ON CONFLICT DO NOTHING;" in out.read_text(encoding="utf-8")
