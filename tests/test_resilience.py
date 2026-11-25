"""Lost connections: every dump must still re-import and match the oracle.

The dump's worker backend is terminated (as a crash, failover, network
failure or admin would) at chosen points. When the snapshot survives, the
interrupted query must run again in the same snapshot, so changes committed in
the meantime must not show up. When it can't, the dump must start over.
"""

from __future__ import annotations

import logging

import psycopg
import pytest

import rt
from intellidump import session as session_mod
from intellidump import source
from intellidump.connection import copied_settings
from intellidump.dump import DumpOptions, run_dump_on
from intellidump.errors import IntellidumpError
from intellidump.session import Session, connection_lost
from rt import ConnectionKiller, needs_db

pytestmark = [pytest.mark.integration, needs_db]

#: g <- p <- c, with enough c rows per p that a lookup streams for a while.
CHAIN_DDL = (
    "CREATE TABLE g (id int PRIMARY KEY, v text); "
    "CREATE TABLE p (id int PRIMARY KEY, g_id int NOT NULL REFERENCES g, v text); "
    "CREATE TABLE c (id int PRIMARY KEY, p_id int NOT NULL REFERENCES p, v text);"
)
CHAIN_DATA = (
    "INSERT INTO g SELECT i, 'g' || i FROM generate_series(1, 3) i; "
    "INSERT INTO p SELECT i, 1 + i % 3, 'p' || i FROM generate_series(1, 6) i; "
    "INSERT INTO c SELECT i, 1 + i % 6, repeat('x', 300) || i FROM generate_series(1, 30000) i;"
)


def _vandalize() -> None:
    """Commit changes that a dump in the old snapshot must not see."""
    with psycopg.connect(rt.DSN, autocommit=True) as other:
        other.execute(
            "DELETE FROM rt.c WHERE id % 2 = 0; DELETE FROM rt.c WHERE p_id IN (1, 2); "
            "DELETE FROM rt.p WHERE id IN (1, 2); UPDATE rt.g SET v = 'changed'; "
            "INSERT INTO rt.c SELECT i, 3, 'new' FROM generate_series(100001, 100100) i;"
        )


@pytest.fixture
def own_conn(rt_target):
    """A dump connection of the test's own, so it can be killed safely."""
    with psycopg.connect(rt.DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture
def chain(env, own_conn):
    env.setup(CHAIN_DDL, CHAIN_DATA)
    env.dump_conn = own_conn
    return env


@pytest.mark.parametrize("after_rows", [0, 1, 5000])
def test_lost_worker_reruns_lookup_in_the_same_snapshot(chain, monkeypatch, after_rows):
    """Killed mid-lookup; the data then changes; the dump must not notice."""
    expected = chain.expected("rt.p", "id IN (1, 2)")
    # Query 1 is the seed, 2 the children of p, so 2 streams 10000 c rows.
    killer = ConnectionKiller(monkeypatch, at={2}, after_rows=after_rows, between=_vandalize)
    result, path = chain.dump("SELECT * FROM rt.p WHERE id IN (1, 2)", fetch_size=100)
    assert killer.kills == 1
    assert (result.reconnects, result.restarts) == (1, 0)
    chain.replay(path)
    chain.assert_target(expected)


def test_every_lookup_losing_its_connection_once(chain, monkeypatch):
    """Each query is killed on its first run: every re-run must succeed."""
    expected = chain.expected("rt.p", "id <= 4")
    first_runs: set[int] = set()

    def first_run_of_each_lookup(n: int) -> bool:
        # Run 1 is the seed. Kill run n unless it re-runs run n - 1, killed before.
        hit = n > 1 and (n - 1) not in first_runs
        if hit:
            first_runs.add(n)
        return hit

    killer = ConnectionKiller(monkeypatch, at=first_run_of_each_lookup, after_rows=3)
    result, path = chain.dump("SELECT * FROM rt.p WHERE id <= 4", batch_size=2, fetch_size=7)
    assert killer.kills >= 4
    # A kill after a query's last row is noticed by the next query, unless none follows.
    assert killer.kills - 1 <= result.reconnects <= killer.kills
    assert result.restarts == 0
    chain.replay(path)
    chain.assert_target(expected)


def test_lost_worker_during_query_starts_over(chain, monkeypatch):
    """The user's own query isn't re-run in place; the dump starts over instead."""
    killer = ConnectionKiller(monkeypatch, at={1}, before=True, between=_vandalize)
    result, path = chain.dump("SELECT * FROM rt.p WHERE id IN (1, 2, 3)")
    assert killer.kills == 1
    assert result.restarts == 1
    # The new snapshot sees the changes: p 1 and 2 are gone.
    chain.replay(path)
    chain.assert_target(chain.expected("rt.p", "id IN (1, 2, 3)"))
    assert [r.split(",")[0] for r in chain.target_state()["rt.p"]] == ["(3"]


def test_lost_snapshot_starts_over(chain, monkeypatch, own_conn):
    """Holder and worker both die (a server restart): start over, consistently."""
    ConnectionKiller(monkeypatch, at={3}, after_rows=2, between=_vandalize, holder=True)
    result, path = chain.dump("SELECT * FROM rt.p WHERE id <= 3")
    assert result.restarts == 1
    assert own_conn.broken
    chain.replay(path)
    chain.assert_target(chain.expected("rt.p", "id <= 3"))


def test_query_that_keeps_losing_its_connection_gives_up(chain, monkeypatch):
    old = chain.tmp / "dump.sql"
    old.write_text("old")
    ConnectionKiller(monkeypatch, at=lambda n: n > 1, before=True)
    with pytest.raises(IntellidumpError, match="lost the connection 3 times"):
        chain.dump("SELECT * FROM rt.p", retries=2)
    assert old.read_text() == "old"
    assert not list((chain.tmp / "work").iterdir())


def test_restarts_are_limited(chain, monkeypatch):
    killer = ConnectionKiller(monkeypatch, at=lambda n: True, before=True)
    with pytest.raises(psycopg.OperationalError):
        chain.dump("SELECT * FROM rt.p", retries=2)
    assert killer.kills == 3  # the first try and two restarts, all killed in the seed query


def test_retries_zero_uses_one_connection_and_fails(chain, monkeypatch, own_conn):
    seen = []
    real = source.PgRowSource._run

    def spy(self, table, stmt, params):
        seen.append((self.session.conn is own_conn, self.session.holder))
        rt.terminate(self.session.conn.info.backend_pid)
        yield from real(self, table, stmt, params)

    monkeypatch.setattr(source.PgRowSource, "_run", spy)
    with pytest.raises(psycopg.OperationalError):
        chain.dump("SELECT * FROM rt.p", retries=0)
    assert seen == [(True, None)]
    assert not (chain.tmp / "dump.sql").exists()


def test_without_losses_the_callers_connection_does_the_work(chain, monkeypatch, own_conn):
    """No behaviour change when nothing goes wrong: the caller's session runs it all."""
    seen = []
    real = source.PgRowSource._run

    def spy(self, table, stmt, params):
        seen.append(self.session.conn is own_conn and self.session.holder is not None)
        yield from real(self, table, stmt, params)

    monkeypatch.setattr(source.PgRowSource, "_run", spy)
    expected = chain.expected("rt.p", "id = 1")
    result, path = chain.dump("SELECT * FROM rt.p WHERE id = 1")
    assert seen and all(seen)
    assert (result.reconnects, result.restarts) == (0, 0)
    assert own_conn.autocommit and own_conn.execute("SELECT 1").fetchone() == (1,)
    chain.replay(path)
    chain.assert_target(expected)


def test_reconnect_waits_for_the_server(chain, monkeypatch):
    """New connections fail for a while (server restarting), then work."""
    sleeps: list[float] = []
    monkeypatch.setattr(session_mod.time, "sleep", sleeps.append)
    state = {"calls": 0}

    def flaky():
        state["calls"] += 1
        # Call 1 opens the holder; calls 2-4 fail like a server starting up.
        if 2 <= state["calls"] <= 4:
            raise psycopg.OperationalError("the database system is starting up")
        return psycopg.connect(rt.DSN)

    expected = chain.expected("rt.p", "id = 1")
    ConnectionKiller(monkeypatch, at={2}, after_rows=1)
    out = chain.tmp / "dump.sql"
    result = run_dump_on(
        chain.dump_conn,
        DumpOptions(query="SELECT * FROM rt.p WHERE id = 1", output=out, schemas=["rt"]),
        connect=flaky,
    )
    assert result.reconnects == 1
    assert sleeps == [1.0, 2.0, 4.0]
    chain.replay(out)
    chain.assert_target(expected)


def _dump_with(chain, connect, **opts):
    return run_dump_on(
        chain.dump_conn,
        DumpOptions(query="SELECT * FROM rt.p", output=chain.tmp / "x.sql", **opts),
        connect=connect,
    )


def test_reconnect_timeout(chain, monkeypatch):
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def dead():
        calls["n"] += 1
        if calls["n"] == 1:
            return psycopg.connect(rt.DSN)
        raise psycopg.OperationalError("connection refused")

    ConnectionKiller(monkeypatch, at={2})
    with pytest.raises(IntellidumpError, match="could not reconnect within 0 s"):
        _dump_with(chain, dead, reconnect_timeout=0)


def test_authentication_failure_is_not_retried(chain, monkeypatch):
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: pytest.fail("retried"))
    calls = {"n": 0}

    def wrong_password():
        calls["n"] += 1
        if calls["n"] == 1:
            return psycopg.connect(rt.DSN)
        return psycopg.connect(rt.DSN, password="wrong", user="rt_nobody")

    ConnectionKiller(monkeypatch, at={2})
    with pytest.raises(psycopg.OperationalError, match="authentication failed"):
        _dump_with(chain, wrong_password)


@pytest.fixture
def reader(env, own_conn):
    """Events whose selection depends on session settings, and a reader role."""
    env.setup(
        "CREATE TABLE ev (id int PRIMARY KEY, at timestamptz, owner text); "
        "CREATE TABLE note (id int PRIMARY KEY, ev_id int REFERENCES ev);",
        "INSERT INTO ev VALUES (1, '2024-01-01 23:30+00', 'a'), (2, '2024-01-02 12:00+00', 'b'); "
        "INSERT INTO note VALUES (1, 1), (2, 2);",
    )
    own_conn.execute("DROP ROLE IF EXISTS rt_reader; CREATE ROLE rt_reader")
    own_conn.execute(
        "GRANT USAGE ON SCHEMA rt TO rt_reader; "
        "GRANT SELECT ON ALL TABLES IN SCHEMA rt TO rt_reader"
    )
    env.dump_conn = own_conn
    yield env
    with psycopg.connect(rt.DSN, autocommit=True) as admin:
        admin.execute("DROP OWNED BY rt_reader; DROP ROLE rt_reader")


def test_callers_session_settings_carry_over(reader, monkeypatch, own_conn):
    """The query relies on the caller's SET search_path, TimeZone and SET ROLE;
    after the caller's connection dies, the new one must have them all."""
    own_conn.execute(
        "SET search_path = rt, public; SET TimeZone = 'Pacific/Kiritimati'; SET ROLE rt_reader"
    )
    users = []
    real = source.PgRowSource._run

    def spy(self, table, stmt, params):
        users.append(self.session.conn.execute("SELECT current_user").fetchone()[0])
        yield from real(self, table, stmt, params)

    monkeypatch.setattr(source.PgRowSource, "_run", spy)
    settings = dict(copied_settings(own_conn))
    assert settings["role"] == "rt_reader" and settings["TimeZone"] == "Pacific/Kiritimati"
    # Kill the caller's connection before the query runs: the dump starts over
    # on a new connection. In Kiritimati (UTC+14) only event 1 falls on
    # 2024-01-02; in the server's UTC only event 2 would. The table name only
    # resolves with the caller's search_path.
    ConnectionKiller(monkeypatch, at={1}, before=True)
    result, path = reader.dump("SELECT * FROM ev WHERE at::date = '2024-01-02'", schemas=())
    assert result.restarts == 1 and own_conn.broken
    assert users and set(users) == {"rt_reader"}
    reader.replay(path)
    reader.assert_target(reader.expected("rt.ev", "id = 1"))


def test_custom_settings_lost_with_the_connection_fail_loudly(reader, monkeypatch, own_conn):
    """PostgreSQL can't list custom settings, so they can't be copied; a query
    that needs one must then fail, not silently select something else."""
    own_conn.execute("SET rt.owner = 'a'")
    ConnectionKiller(monkeypatch, at={1}, before=True)
    with pytest.raises(psycopg.errors.UndefinedObject, match=r"rt.owner"):
        reader.dump("SELECT * FROM rt.ev WHERE owner = current_setting('rt.owner')")


def test_cli_reconnects_without_prompting_again(chain, monkeypatch, tmp_path):
    """Through the CLI with only environment variables, including the password."""
    from psycopg.conninfo import conninfo_to_dict

    from intellidump.cli import main

    parts = conninfo_to_dict(rt.DSN)
    for var, key in [
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGUSER", "user"),
        ("PGPASSWORD", "password"),
        ("PGDATABASE", "dbname"),
    ]:
        if key in parts:
            monkeypatch.setenv(var, str(parts[key]))
    monkeypatch.setattr("getpass.getpass", lambda *a: pytest.fail("prompted"))
    expected = chain.expected("rt.p", "id = 2")
    killer = ConnectionKiller(monkeypatch, at={2, 4}, after_rows=1, between=_vandalize)
    out = tmp_path / "cli.sql"
    code = main(["-f", str(out), "-n", "rt", "-q", "SELECT * FROM rt.p WHERE id = 2", "-w"])
    assert code == 0 and killer.kills == 2
    chain.replay(out)
    chain.assert_target(expected)


def test_warnings_are_logged(chain, monkeypatch, caplog):
    ConnectionKiller(monkeypatch, at={1, 3}, before=True)
    with caplog.at_level(logging.WARNING, logger="intellidump"):
        result, _ = chain.dump("SELECT * FROM rt.p WHERE id = 1")
    assert (result.restarts, result.reconnects) == (1, 1)
    text = caplog.text
    assert "starting the dump over in a new snapshot (restart 1 of 5)" in text
    assert "run the query again in the same snapshot (retry 1 of 5)" in text


def test_connection_lost_classification(own_conn):
    with pytest.raises(psycopg.Error) as exc:
        own_conn.execute("SELECT 1/0")
    assert not connection_lost(exc.value, own_conn)
    rt.terminate(own_conn.info.backend_pid)
    with pytest.raises(psycopg.Error) as exc:
        own_conn.execute("SELECT 1")
    assert connection_lost(exc.value, own_conn)
    assert not connection_lost(ValueError("x"))


def test_session_without_a_second_connection_still_works(own_conn, caplog):
    """If a second connection can't be had, the dump still runs (restarts only)."""

    def no_more():
        raise psycopg.OperationalError("sorry, too many clients already")

    s = Session(no_more, first=own_conn)
    with caplog.at_level(logging.WARNING, logger="intellidump"):
        s.start()
    try:
        assert s.holder is None and s.conn is own_conn
        row = own_conn.execute("SELECT current_setting('transaction_isolation')").fetchone()
        assert row == ("repeatable read",)
        assert "could not keep the snapshot on a second connection" in caplog.text
    finally:
        s.close()


def test_new_worker_resolves_schema_local_types(env, monkeypatch, own_conn):
    """Lookups cast to the key type by its search_path-relative name; a new
    worker must get the dump's search_path or the cast fails."""
    env.setup(
        "CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy'); "
        "CREATE DOMAIN code AS text CHECK (VALUE <> ''); "
        "CREATE TABLE kind (m mood, c code, PRIMARY KEY (m, c)); "
        "CREATE TABLE item (id int PRIMARY KEY, m mood, c code, "
        "FOREIGN KEY (m, c) REFERENCES kind);"
        "CREATE TABLE tag (id int PRIMARY KEY, item_id int REFERENCES item);",
        "INSERT INTO kind VALUES ('sad', 'x'), ('ok', 'y'), ('happy', 'z'); "
        "INSERT INTO item VALUES (1, 'ok', 'y'), (2, 'happy', 'z'), (3, 'sad', 'x'); "
        "INSERT INTO tag VALUES (1, 1), (2, 2);",
    )
    env.dump_conn = own_conn
    expected = env.expected("rt.tag", "id <= 2")
    # 1: seed, 2: tag -> item, 3: item -> kind (cast to mood and code).
    ConnectionKiller(monkeypatch, at={3}, before=True)
    result, path = env.dump("SELECT * FROM rt.tag WHERE id <= 2")
    assert result.reconnects == 1
    env.replay(path)
    env.assert_target(expected)


def test_second_connection_on_another_server_falls_back(chain, monkeypatch, own_conn, caplog):
    """A proxy or multi-host conninfo can send the second connection elsewhere,
    where the snapshot means nothing: the dump must still work, on one connection."""
    # Every backend looks like a different server.
    monkeypatch.setattr(session_mod, "_SERVER_IDENTITY", "pg_catalog.pg_backend_pid()::text")
    expected = chain.expected("rt.p", "id = 1")
    with caplog.at_level(logging.WARNING, logger="intellidump"):
        _, path = chain.dump("SELECT * FROM rt.p WHERE id = 1")
    assert "reached a different server" in caplog.text
    assert own_conn.execute("SELECT 1").fetchone() == (1,)
    chain.replay(path)
    chain.assert_target(expected)


def test_reconnect_to_another_server_starts_over(chain, monkeypatch):
    """After a failover the new worker can reach a different server than the
    holder: the held snapshot isn't valid there, so the dump must start over."""
    calls = {"n": 0}
    real_join = Session._join

    def join(self, conn, settings):
        calls["n"] += 1
        if calls["n"] == 2:  # the first replacement worker
            self._server = "elsewhere"
        return real_join(self, conn, settings)

    monkeypatch.setattr(Session, "_join", join)
    ConnectionKiller(monkeypatch, at={2}, after_rows=1)
    expected = chain.expected("rt.p", "id = 1")
    result, path = chain.dump("SELECT * FROM rt.p WHERE id = 1")
    assert result.restarts == 1
    chain.replay(path)
    chain.assert_target(expected)
