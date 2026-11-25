"""Command line interface, modelled on pg_dump's options."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import psycopg

from .connection import ConnectionOptions
from .dump import DumpOptions, run_dump
from .errors import IntellidumpError
from .store import DEFAULT_CACHE_MB

PROG = "intellidump"

DESCRIPTION = """\
Export the rows selected by a query as INSERT statements, together with every
row that depends on them through foreign keys (recursively) and every row they
reference, ordered so the file imports cleanly with constraints enabled.
"""

EPILOG = """\
Connection options not given fall back to the usual libpq environment
variables (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSERVICE, ...)
and the password file, exactly like pg_dump.

example:
  intellidump -h db.example.com -U app -d prod -n public -f last_week.sql \\
      --on-conflict-do-nothing \\
      --query "SELECT * FROM prediction WHERE created > now() - interval '7 days'"
"""


def _positive_int(text: str) -> int:
    """Parse an integer of at least 1 for argparse.

    Args:
        text: Raw option value.

    Returns:
        The parsed integer.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text!r}")
    return value


def _non_negative_int(text: str) -> int:
    """Parse an integer of at least 0 for argparse.

    Args:
        text: Raw option value.

    Returns:
        The parsed integer.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {text!r}")
    return value


def _seconds(text: str) -> float:
    """Parse a non-negative number of seconds for argparse.

    Args:
        text: Raw option value.

    Returns:
        The number of seconds.
    """
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {text!r}") from None
    if not value >= 0 or value == float("inf"):
        raise argparse.ArgumentTypeError(f"must be a non-negative number: {text!r}")
    return value


def _port(text: str) -> int:
    """Parse a TCP port number for argparse.

    Args:
        text: Raw option value.

    Returns:
        The port, 1 to 65535.
    """
    value = _positive_int(text)
    if value > 65535:
        raise argparse.ArgumentTypeError(f"invalid port: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        A parser with pg_dump-style options: ``-h`` is the host, help is ``-?``.
    """
    from . import __version__

    # add_help=False: like pg_dump, -h means --host. Help is --help / -?.
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    general = parser.add_argument_group("general options")
    general.add_argument("-?", "--help", action="help", help="show this help, then exit")
    general.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    general.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="verbose progress messages on stderr (repeat for debug output)",
    )
    general.add_argument(
        "-f", "--file", required=True, metavar="FILENAME", help="output .sql file (required)"
    )

    selection = parser.add_argument_group("row selection")
    query = selection.add_mutually_exclusive_group(required=True)
    query.add_argument(
        "-q",
        "--query",
        metavar="SQL",
        help="SELECT statement choosing the rows to export",
    )
    query.add_argument(
        "--query-file",
        metavar="PATH",
        type=Path,
        help="read the SELECT statement from a file",
    )
    selection.add_argument(
        "-t",
        "--table",
        metavar="TABLE",
        help="the table the query's rows belong to (only needed when the query joins "
        "several tables)",
    )
    selection.add_argument(
        "-n",
        "--schema",
        metavar="SCHEMA",
        action="append",
        default=[],
        help="schema(s) whose tables are followed; repeatable (default: the schema of the "
        "query's table)",
    )
    selection.add_argument(
        "-T",
        "--exclude-table",
        metavar="TABLE",
        action="append",
        default=[],
        help="never export rows of this table; repeatable",
    )

    traversal = parser.add_argument_group("dependency traversal")
    traversal.add_argument(
        "--no-children",
        dest="follow_children",
        action="store_false",
        help="do not export rows that reference the selected rows",
    )
    traversal.add_argument(
        "--no-parents",
        dest="follow_parents",
        action="store_false",
        help="do not export rows that the exported rows reference (the target database "
        "must already contain them)",
    )
    traversal.add_argument(
        "--max-rows",
        metavar="N",
        type=_positive_int,
        help="abort if more than N rows would be exported",
    )
    traversal.add_argument(
        "--work-dir",
        metavar="DIR",
        help="directory for the temporary work database that holds collected rows "
        "(default: the system temp directory); needs about as much free space as the dump",
    )
    traversal.add_argument(
        "--cache-size",
        metavar="MB",
        type=_positive_int,
        default=DEFAULT_CACHE_MB,
        help="memory for the work database's page cache (default: %(default)s); rows "
        "beyond it stay on disk",
    )
    traversal.add_argument(
        "--fetch-size",
        metavar="N",
        type=_positive_int,
        default=1000,
        help="rows per round trip when streaming from PostgreSQL (default: %(default)s)",
    )
    traversal.add_argument(
        "--batch-size",
        metavar="N",
        type=_positive_int,
        default=1000,
        help="key values per lookup query (default: %(default)s)",
    )

    output = parser.add_argument_group("output options")
    output.add_argument(
        "--on-conflict-do-nothing",
        action="store_true",
        help="add ON CONFLICT DO NOTHING to each INSERT so rows already imported by an "
        "earlier run are skipped",
    )
    output.add_argument(
        "--transaction",
        action="store_true",
        help="wrap the script in BEGIN; ... COMMIT;",
    )
    output.add_argument(
        "--rows-per-insert",
        metavar="N",
        type=_positive_int,
        default=1,
        help="rows per INSERT statement (default: %(default)s)",
    )
    output.add_argument(
        "--output-schema",
        metavar="[SOURCE=]TARGET",
        action="append",
        default=[],
        help="write INSERTs into schema TARGET instead of the source schema; use "
        "SOURCE=TARGET (repeatable) to map several dumped schemas separately",
    )
    output.add_argument(
        "--set-sequences",
        action="store_true",
        help="advance serial/identity sequences past the imported values (never moves "
        "them backwards)",
    )

    conn = parser.add_argument_group("connection options")
    conn.add_argument(
        "-d",
        "--dbname",
        metavar="DBNAME",
        help="database name, or a conninfo string / postgresql:// URI",
    )
    conn.add_argument(
        "-h", "--host", metavar="HOSTNAME", help="database server host or socket directory"
    )
    conn.add_argument("-p", "--port", metavar="PORT", type=_port, help="database server port")
    conn.add_argument("-U", "--username", metavar="NAME", help="database user name")
    conn.add_argument(
        "--password",
        metavar="PASSWORD",
        help="password (visible to other local users in the process list; prefer "
        "PGPASSWORD or a password file)",
    )
    pw = conn.add_mutually_exclusive_group()
    pw.add_argument(
        "-W",
        "--password-prompt",
        action="store_true",
        help="prompt for the password before connecting",
    )
    pw.add_argument("-w", "--no-password", action="store_true", help="never prompt for a password")
    conn.add_argument(
        "--retries",
        metavar="N",
        type=_non_negative_int,
        default=5,
        help="after a lost connection, reconnect and run the interrupted query again in the "
        "same snapshot up to N times, and start the dump over up to N times if the snapshot "
        "is lost too; 0 uses one connection and fails at once (default: %(default)s)",
    )
    conn.add_argument(
        "--reconnect-timeout",
        metavar="SECONDS",
        type=_seconds,
        default=300.0,
        help="how long to keep trying to reconnect after losing the connection "
        "(default: %(default)g)",
    )
    return parser


def _configure_logging(verbosity: int) -> None:
    """Send intellidump's log messages to stderr.

    Args:
        verbosity: 0 for warnings only, 1 for progress, 2 or more for debug output.
    """
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(f"{PROG}: %(levelname)s: %(message)s"))
    root = logging.getLogger("intellidump")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def options_from_args(args: argparse.Namespace) -> DumpOptions:
    """Turn parsed command line arguments into dump options.

    Args:
        args: Result of ``build_parser().parse_args()``.

    Returns:
        Options for run_dump.
    """
    if args.query_file is not None:
        try:
            query = args.query_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise IntellidumpError(f"cannot read query file: {exc}") from None
    else:
        query = args.query
    return DumpOptions(
        query=query,
        output=args.file,
        connection=ConnectionOptions(
            host=args.host,
            port=args.port,
            username=args.username,
            password=args.password,
            dbname=args.dbname,
            prompt_password=args.password_prompt,
            no_password=args.no_password,
        ),
        schemas=args.schema,
        table=args.table,
        exclude_tables=args.exclude_table,
        follow_children=args.follow_children,
        follow_parents=args.follow_parents,
        on_conflict_do_nothing=args.on_conflict_do_nothing,
        transaction=args.transaction,
        rows_per_insert=args.rows_per_insert,
        set_sequences=args.set_sequences,
        output_schemas=args.output_schema,
        batch_size=args.batch_size,
        fetch_size=args.fetch_size,
        work_dir=args.work_dir,
        cache_mb=args.cache_size,
        max_rows=args.max_rows,
        retries=args.retries,
        reconnect_timeout=args.reconnect_timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line tool.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` if None.

    Returns:
        Process exit code: 0 on success, 1 on error, 130 when interrupted.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        result = run_dump(options_from_args(args))
    except IntellidumpError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        message = str(exc).strip() or type(exc).__name__
        print(f"{PROG}: error: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130
    if args.verbose:
        print(
            f"{PROG}: exported {result.total_rows} row(s) from {len(result.counts)} table(s) "
            f"to {args.file} using {result.queries} queries",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
