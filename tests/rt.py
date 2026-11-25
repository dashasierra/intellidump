"""Round-trip test harness: dump from a source database, replay into an empty
target database, and compare every row with an independently computed oracle.

Needs ``INTELLIDUMP_TEST_DSN`` (a role allowed to create databases, roles and
extensions; the tests create and drop ``intellidump_rt_target``).

Set ``INTELLIDUMP_TEST_PSQL`` to replay dumps through the real ``psql`` client
instead of psycopg, e.g.::

    INTELLIDUMP_TEST_PSQL="podman exec -i intellidump-test-pg psql -U postgres -d {dbname}"
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from intellidump.dump import DumpOptions, DumpResult, run_dump_on

DSN = os.environ.get("INTELLIDUMP_TEST_DSN")
PSQL = os.environ.get("INTELLIDUMP_TEST_PSQL")
TARGET_DB = "intellidump_rt_target"

needs_db = pytest.mark.skipif(not DSN, reason="INTELLIDUMP_TEST_DSN is not set")

#: Settings used whenever rows are compared, so text forms are comparable.
COMPARE_SETTINGS = (
    "SET TimeZone = 'UTC'; SET DateStyle = 'ISO'; SET IntervalStyle = 'postgres'; "
    "SET extra_float_digits = 3; SET bytea_output = 'hex'"
)


def target_dsn() -> str:
    assert DSN
    return make_conninfo(DSN, dbname=TARGET_DB)


def create_target_database() -> None:
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TARGET_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TARGET_DB}")


def drop_target_database() -> None:
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TARGET_DB} WITH (FORCE)")


@dataclass
class Env:
    src: psycopg.Connection[Any]
    dst: psycopg.Connection[Any]
    schema: str
    tmp: Path
    #: Connection dumps run on, when not ``src`` (tests that kill it set this).
    dump_conn: psycopg.Connection[Any] | None = None

    # -- setup -------------------------------------------------------------

    def setup(self, ddl: str, data: str = "") -> None:
        """Create the schema in both databases; load ``data`` into the source only."""
        q = psycopg.sql.Identifier(self.schema).as_string(self.src)
        for conn in (self.src, self.dst):
            conn.execute(f"DROP SCHEMA IF EXISTS {q} CASCADE")
            conn.execute(f"CREATE SCHEMA {q}")
            conn.execute(f"SET search_path = {q}, public")
            conn.execute(ddl)
            conn.execute("RESET search_path")
        if data:
            self.src.execute(f"SET search_path = {q}, public")
            self.src.execute(data)
            self.src.execute("RESET search_path")

    # -- dump and replay ---------------------------------------------------

    def dump(self, query: str, name: str = "dump.sql", **opts: Any) -> tuple[DumpResult, Path]:
        out = self.tmp / name
        opts.setdefault("schemas", [self.schema])
        opts.setdefault("work_dir", self.tmp / "work")
        conn = self.dump_conn or self.src
        result = run_dump_on(conn, DumpOptions(query=query, output=out, **opts))
        assert not list((self.tmp / "work").iterdir()), "work database left behind"
        data = out.read_bytes()
        # Line-safe: no raw control characters, tabs only as multi-row indentation.
        assert not re.search(rb"[\x00-\x08\x0b-\x1f\x7f]", data), "raw control character"
        assert not re.search(rb"[^\n]\t", data), "raw tab inside a statement"
        return result, out

    def replay(self, path: Path, *, session_sql: str = "", crlf: bool = False) -> None:
        """Run a dump file against the target database.

        ``crlf`` first rewrites the file with Windows line endings, as git
        autocrlf or an editor might; the data must not change.
        """
        if crlf:
            mangled = path.with_suffix(".crlf.sql")
            mangled.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            path = mangled
        if PSQL:
            cmd = [*shlex.split(PSQL.format(dbname=TARGET_DB)), "-X", "-q", "-v", "ON_ERROR_STOP=1"]
            script = (session_sql + ";\n" if session_sql else "").encode() + path.read_bytes()
            proc = subprocess.run(cmd, input=script, capture_output=True, timeout=600)
            if proc.returncode != 0:
                raise AssertionError(
                    f"psql failed ({proc.returncode}):\n{proc.stderr.decode(errors='replace')}"
                )
            return
        try:
            if session_sql:
                self.dst.execute(session_sql)
            self.dst.execute(path.read_bytes().decode("utf-8"))
        finally:
            if self.dst.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                self.dst.execute("ROLLBACK")
            self.dst.execute("RESET ALL")

    # -- comparing ---------------------------------------------------------

    def tables(self) -> list[tuple[int, str, str]]:
        """``(oid, quoted name, relkind)`` of the source schema's tables."""
        return self.src.execute(
            """SELECT c.oid::int8, quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                      c.relkind
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
               ORDER BY c.relname""",
            (self.schema,),
        ).fetchall()

    @staticmethod
    def _from(name: str, kind: str) -> str:
        return f"{'ONLY ' if kind == 'r' else ''}{name}"

    def rows(self, conn: psycopg.Connection[Any], name: str, kind: str) -> list[str]:
        conn.execute(COMPARE_SETTINGS)
        try:
            recs = conn.execute(f"SELECT t::text FROM {self._from(name, kind)} t").fetchall()
        finally:
            conn.execute("RESET ALL")
        return sorted(r[0] for r in recs)

    def target_state(self) -> dict[str, list[str]]:
        return {name: self.rows(self.dst, name, kind) for _, name, kind in self.tables()}

    def assert_target(self, expected: dict[str, list[str]]) -> None:
        """The target holds exactly ``expected`` (missing tables: no rows)."""
        got = self.target_state()
        problems = []
        for name, rows in got.items():
            want = expected.get(name, [])
            if Counter(rows) != Counter(want):
                missing = sorted((Counter(want) - Counter(rows)).elements())[:5]
                extra = sorted((Counter(rows) - Counter(want)).elements())[:5]
                problems.append(f"{name}: missing {missing} extra {extra}")
        assert not problems, "target differs from expected:\n" + "\n".join(problems)

    # -- the oracle --------------------------------------------------------

    def expected(
        self,
        root: str,
        where: str = "true",
        *,
        children: bool = True,
        parents: bool = True,
    ) -> dict[str, list[str]]:
        """Rows a correct dump contains, computed straight from the source.

        Every foreign key's (child row, parent row) pairs come from a join in
        PostgreSQL, so equality follows PostgreSQL's own semantics (numeric
        scale, citext, interval normalisation, char padding, ...).
        """
        src = self.src
        tables = {oid: (name, kind) for oid, name, kind in self.tables()}
        src.execute(COMPARE_SETTINGS)
        try:
            text: dict[tuple[int, str], str] = {}
            for oid, (name, kind) in tables.items():
                for ctid, row in src.execute(
                    f"SELECT tableoid::int8 || ':' || ctid::text, t::text "
                    f"FROM {self._from(name, kind)} t"
                ):
                    text[(oid, ctid)] = row
            fks = src.execute(
                """SELECT conrelid::int8, confrelid::int8,
                          ARRAY(SELECT quote_ident(attname) FROM unnest(conkey) WITH ORDINALITY k(n, o)
                                JOIN pg_attribute ON attrelid = conrelid AND attnum = k.n ORDER BY o),
                          ARRAY(SELECT quote_ident(attname) FROM unnest(confkey) WITH ORDINALITY k(n, o)
                                JOIN pg_attribute ON attrelid = confrelid AND attnum = k.n ORDER BY o)
                   FROM pg_constraint
                   WHERE contype = 'f' AND conparentid = 0
                     AND conrelid = ANY(%s) AND confrelid = ANY(%s)""",
                (list(tables), list(tables)),
            ).fetchall()
            down: dict[tuple[int, str], set[tuple[int, str]]] = defaultdict(set)
            up: dict[tuple[int, str], set[tuple[int, str]]] = defaultdict(set)
            for child, parent, ccols, pcols in fks:
                cname, ckind = tables[child]
                pname, pkind = tables[parent]
                cond = " AND ".join(f"c.{a} = p.{b}" for a, b in zip(ccols, pcols, strict=True))
                for c_id, p_id in src.execute(
                    f"SELECT c.tableoid::int8 || ':' || c.ctid::text, "
                    f"p.tableoid::int8 || ':' || p.ctid::text "
                    f"FROM {self._from(cname, ckind)} c JOIN {self._from(pname, pkind)} p ON {cond}"
                ):
                    c_node, p_node = (child, c_id), (parent, p_id)
                    down[p_node].add(c_node)
                    up[c_node].add(p_node)
            root_oid = next(o for o, (n, _) in tables.items() if n == root)
            rname, rkind = tables[root_oid]
            seeds = {
                (root_oid, r[0])
                for r in src.execute(
                    f"SELECT tableoid::int8 || ':' || ctid::text FROM {self._from(rname, rkind)} "
                    f"WHERE {where}"
                )
            }
        finally:
            src.execute("RESET ALL")

        def close(start: set[tuple[int, str]], edges: dict) -> set[tuple[int, str]]:
            seen, todo = set(start), list(start)
            while todo:
                for nxt in edges.get(todo.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        todo.append(nxt)
            return seen

        rows = close(seeds, down) if children else set(seeds)
        if parents:
            rows = close(rows, up)
        out: dict[str, list[str]] = defaultdict(list)
        for oid, ctid in rows:
            out[tables[oid][0]].append(text[(oid, ctid)])
        return {k: sorted(v) for k, v in out.items()}

    # -- everything at once ------------------------------------------------

    def roundtrip(
        self,
        root: str,
        where: str = "true",
        *,
        query: str | None = None,
        replay_twice: bool = False,
        crlf: bool = False,
        session_sql: str = "",
        source_sql: str = "",
        **opts: Any,
    ) -> tuple[DumpResult, Path]:
        """Dump ``root`` rows matching ``where``, replay, and check the target.

        ``source_sql`` runs on the source connection just before the dump (to give
        it unusual session settings); ``session_sql`` runs on the target first.
        """
        expected = self.expected(
            root,
            where,
            children=opts.get("follow_children", True),
            parents=opts.get("follow_parents", True),
        )
        if source_sql:
            self.src.execute(source_sql)
        try:
            result, path = self.dump(query or f"SELECT * FROM {root} WHERE {where}", **opts)
        finally:
            self.src.execute("RESET ALL")
        self.replay(path, session_sql=session_sql, crlf=crlf)
        self.assert_target(expected)
        if replay_twice:
            self.replay(path, session_sql=session_sql, crlf=crlf)
            self.assert_target(expected)
        return result, path


def terminate(pid: int) -> None:
    """Kill a backend, as a server crash, failover or admin would, and wait for it."""
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s, 10000)", (pid,))


class ConnectionKiller:
    """Terminates the dump's worker backend during chosen row queries.

    Queries are counted from 1 in the order PgRowSource runs them (the seed query
    first), re-runs included. During query ``n`` in ``at``, the worker is killed
    after it has returned ``after_rows`` rows (or when it ends, if it has fewer),
    and ``between`` runs once (for example to change data before the reconnect).
    ``before`` kills it before the query is sent instead, so the query itself
    fails. ``holder`` also kills the connection holding the snapshot.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        at: Any,
        *,
        after_rows: int = 0,
        between: Any = None,
        before: bool = False,
        holder: bool = False,
    ) -> None:
        from intellidump import source

        self.at = set(at) if not callable(at) else at
        self.after_rows = after_rows
        self.between = between
        self.before = before
        self.holder = holder
        self.runs = 0
        self.kills = 0
        real = source.PgRowSource._run
        killer = self

        def run(src: Any, table: Any, stmt: Any, params: Any) -> Any:
            killer.runs += 1
            hit = killer.runs in killer.at if not callable(killer.at) else killer.at(killer.runs)
            if not hit:
                yield from real(src, table, stmt, params)
                return
            if killer.before:
                killer.kill(src.session)
                yield from real(src, table, stmt, params)
                return
            done = False
            for i, item in enumerate(real(src, table, stmt, params)):
                if i == killer.after_rows and not done:
                    killer.kill(src.session)
                    done = True
                yield item
            if not done:
                killer.kill(src.session)

        monkeypatch.setattr(source.PgRowSource, "_run", run)

    def kill(self, session: Any) -> None:
        self.kills += 1
        pids = [session.conn.info.backend_pid]
        if self.holder and session.holder is not None:
            pids.append(session.holder.info.backend_pid)
        for pid in pids:
            terminate(pid)
        if self.between:
            between, self.between = self.between, None
            between()


def dbname_of(dsn: str) -> str:
    return conninfo_to_dict(dsn).get("dbname", "postgres")
