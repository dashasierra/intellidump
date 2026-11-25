from __future__ import annotations

import pytest

from intellidump.cli import build_parser, main, options_from_args
from intellidump.connection import ConnectionOptions, build_connect_args
from intellidump.errors import IntellidumpError
from intellidump.source import normalize_query


def parse(*argv: str):
    return options_from_args(build_parser().parse_args(list(argv)))


def test_pg_dump_style_connection_flags():
    opts = parse(
        "-h",
        "db",
        "-p",
        "6543",
        "-U",
        "me",
        "-d",
        "app",
        "--password",
        "s3cret",
        "-n",
        "public",
        "-n",
        "audit",
        "-f",
        "out.sql",
        "-q",
        "SELECT * FROM x",
    )
    assert opts.connection == ConnectionOptions(
        host="db", port=6543, username="me", password="s3cret", dbname="app"
    )
    assert opts.schemas == ["public", "audit"]
    assert opts.output == "out.sql"


def test_defaults():
    opts = parse("-f", "o.sql", "--query", "SELECT 1")
    assert opts.follow_children and opts.follow_parents
    assert not opts.on_conflict_do_nothing
    assert not opts.transaction
    assert opts.rows_per_insert == 1
    assert opts.output_schemas == []
    assert opts.connection == ConnectionOptions()


def test_flags():
    opts = parse(
        "-f",
        "o.sql",
        "-q",
        "SELECT 1",
        "--on-conflict-do-nothing",
        "--transaction",
        "--no-parents",
        "--no-children",
        "-T",
        "audit_log",
        "--rows-per-insert",
        "50",
        "--set-sequences",
        "--max-rows",
        "10",
        "-t",
        "prediction",
    )
    assert opts.on_conflict_do_nothing and opts.transaction and opts.set_sequences
    assert not opts.follow_parents and not opts.follow_children
    assert opts.exclude_tables == ["audit_log"]
    assert opts.rows_per_insert == 50 and opts.max_rows == 10
    assert opts.table == "prediction"


def test_query_file(tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("SELECT * FROM t;\n", encoding="utf-8")
    assert parse("-f", "o.sql", "--query-file", str(q)).query == "SELECT * FROM t;\n"


def test_output_file_is_required(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["-q", "SELECT 1"])
    assert "--file" in capsys.readouterr().err


def test_query_is_required(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["-f", "o.sql"])


def test_help_is_question_mark(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["-?"])
    assert exc.value.code == 0
    assert "--on-conflict-do-nothing" in capsys.readouterr().out


def test_connection_error_is_reported(capsys, monkeypatch):
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    code = main(["-h", "127.0.0.1", "-p", "1", "-d", "x", "-w", "-f", "o.sql", "-q", "SELECT 1"])
    assert code == 1
    assert capsys.readouterr().err.startswith("intellidump: error:")


def test_conninfo_dbname_with_overrides(monkeypatch):
    monkeypatch.delenv("PGAPPNAME", raising=False)
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    conninfo, kwargs = build_connect_args(
        ConnectionOptions(dbname="postgresql://u@h/db?connect_timeout=5", port=5433)
    )
    assert conninfo == "postgresql://u@h/db?connect_timeout=5"
    assert kwargs == {
        "port": 5433,
        "application_name": "intellidump",
        "keepalives": 1,
        "keepalives_idle": 60,
        "keepalives_interval": 10,
        "keepalives_count": 6,
    }


def test_conninfo_keepalives_win(monkeypatch):
    _, kwargs = build_connect_args(ConnectionOptions(dbname="host=h keepalives_idle=5"))
    assert "keepalives_idle" not in kwargs
    assert kwargs["keepalives_interval"] == 10


@pytest.mark.parametrize(
    ("argv", "retries", "timeout"),
    [([], 5, 300.0), (["--retries", "0"], 0, 300.0), (["--reconnect-timeout", "2.5"], 5, 2.5)],
)
def test_retry_options(argv, retries, timeout):
    args = build_parser().parse_args(["-f", "o.sql", "-q", "SELECT 1", *argv])
    opts = options_from_args(args)
    assert (opts.retries, opts.reconnect_timeout) == (retries, timeout)


@pytest.mark.parametrize("argv", [["--retries", "-1"], ["--reconnect-timeout", "nan"]])
def test_retry_options_rejected(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["-f", "o.sql", "-q", "SELECT 1", *argv])


def test_plain_dbname_gets_default_timeout(monkeypatch):
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    _, kwargs = build_connect_args(ConnectionOptions(dbname="app"))
    assert kwargs["dbname"] == "app"
    assert kwargs["connect_timeout"] == 30


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SELECT 1;", "SELECT 1"),
        ("  SELECT 1 ; ;\n", "SELECT 1"),
        ("SELECT ';'", "SELECT ';'"),
    ],
)
def test_normalize_query(raw, expected):
    assert normalize_query(raw) == expected


def test_empty_query():
    with pytest.raises(IntellidumpError):
        normalize_query(" ; ")


def test_output_schema_flag():
    opts = parse(
        "-f",
        "o.sql",
        "-q",
        "SELECT 1",
        "--output-schema",
        "custom",
        "--output-schema",
        "audit=audit_copy",
    )
    assert opts.output_schemas == ["custom", "audit=audit_copy"]


def test_memory_flags():
    opts = parse(
        "-f",
        "o.sql",
        "-q",
        "SELECT 1",
        "--work-dir",
        "D:/scratch",
        "--cache-size",
        "1024",
        "--fetch-size",
        "5000",
    )
    assert opts.work_dir == "D:/scratch"
    assert opts.cache_mb == 1024
    assert opts.fetch_size == 5000
    defaults = parse("-f", "o.sql", "-q", "SELECT 1")
    assert defaults.work_dir is None and defaults.cache_mb == 256
