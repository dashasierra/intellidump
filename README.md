# intellidump

Intelligent Dump of PgSQL Data.

`intellidump` works like `pg_dump --data-only --inserts`, but you give it a `SELECT` query
instead of whole tables. It writes a `.sql` file holding:

1. the rows your query selects,
2. every row in the same schema that **depends on** those rows through a foreign key,
   followed recursively (rows that reference them, rows that reference *those*, and so on),
3. every row any exported row **references** (for example the `model` a `prediction` belongs
   to), so the file imports cleanly into an empty database.

Rows are written **in creation order**: a referenced row always comes before the rows that
point at it, so the file can be replayed with every foreign key constraint enabled.

## Example

Say `entity.prediction_id` references `prediction.id` and `action.entity_id` references
`entity.id`:

```
intellidump -h db.example.com -U app -d prod -f last_week.sql --on-conflict-do-nothing \
    --query "SELECT * FROM prediction WHERE created > CURRENT_TIMESTAMP - INTERVAL '7 days'"
```

`last_week.sql` then contains, in this order:

* the `model` (and any other) rows those predictions reference,
* the predictions from the last 7 days,
* only the `entity` rows belonging to *those* predictions,
* only the `action` rows belonging to *those* entities, plus anything the actions reference.

Rows that are pulled in only because something references them (like the `model`) do
**not** drag in their own other dependents, so you don't get every other prediction of that model.

Import it with `psql`:

```
psql -h staging -d app -f last_week.sql
```

## Installation

Requires Python 3.10+ and PostgreSQL 11+ on the server. psycopg 3 ships its own libpq, so no
PostgreSQL client install is needed.

A dump from an older server imports into a newer one, which makes intellidump usable for moving
data off a PostgreSQL 11 server. On 11, floating-point values are written with 17 significant
digits (`0.1` comes out as `0.100000000000000006`) rather than the shortest exact form newer
servers use; they read back as exactly the same values.

```
pip install .            # or: pip install -e ".[dev]" for development
```

This installs an `intellidump` command; `python -m intellidump` works too.

## Options

| Option | Meaning |
| --- | --- |
| `-q, --query SQL` / `--query-file PATH` | The `SELECT` choosing the rows to export (one is required). Its output must include the primary key (or a unique key) of the table it selects from; `SELECT *` always does. |
| `-f, --file FILENAME` | Output file (required). Written atomically, so a failed run never leaves a half-written file. |
| `-t, --table TABLE` | Which table the query's rows belong to. Needed when the query joins several tables and selects key columns from more than one, or when PostgreSQL can't trace output columns back to a table (`UNION`, `id::bigint`). The key columns are then found by name. |
| `-n, --schema SCHEMA` | Schema(s) whose tables are followed; repeatable. Default: the schema of the query's table. Also put first on the `search_path`, so the query can use unqualified names. |
| `-T, --exclude-table TABLE` | Never export rows of this table, nor follow foreign keys through it; repeatable. |
| `--no-children` | Don't export rows that reference the selected rows. |
| `--no-parents` | Don't export rows that the exported rows reference (the target must already have them). |
| `--on-conflict-do-nothing` | Append `ON CONFLICT DO NOTHING` to every `INSERT`, so rows an earlier import already brought in are skipped instead of failing the import. |
| `--transaction` | Wrap the script in `BEGIN;` … `COMMIT;`. Off by default. |
| `--rows-per-insert N` | Rows per `INSERT` statement (default 1, like pg_dump). |
| `--output-schema [SOURCE=]TARGET` | Write the statements into schema `TARGET` instead of the source schema, e.g. `--output-schema custom` turns `public.tablename` into `"custom"."tablename"`. When dumping several schemas, use `SOURCE=TARGET` (repeatable) to map each one, optionally plus a plain `TARGET` for the rest. The target tables must already exist. |
| `--set-sequences` | Move serial/identity sequences past the imported ids. They only ever move forward. The sequence is looked up on the target table at import time, so this works with `--output-schema` too. |
| `--max-rows N` | Stop with an error if more than N rows would be exported. A guard against queries that turn out to reach far more data than expected. |
| `--batch-size N` | Key values per lookup query (default 1000). |
| `--work-dir DIR` | Where the temporary work database goes (default: the system temp directory). See [Memory and disk use](#memory-and-disk-use). |
| `--cache-size MB` | Memory for the work database's page cache (default 256). |
| `--fetch-size N` | Rows per round trip when streaming results from PostgreSQL (default 1000). |
| `-v, --verbose` | Progress on stderr; `-vv` for debug output. |

### Connection

| Option | Environment fallback |
| --- | --- |
| `-h, --host HOSTNAME` | `PGHOST` |
| `-p, --port PORT` | `PGPORT` |
| `-U, --username NAME` | `PGUSER` |
| `--password PASSWORD` | `PGPASSWORD`, then the password file (`~/.pgpass`, `%APPDATA%\postgresql\pgpass.conf` on Windows) |
| `-d, --dbname DBNAME` | `PGDATABASE`. May also be a conninfo string or `postgresql://` URI, as with pg_dump. |
| `-W, --password-prompt` / `-w, --no-password` | Always / never prompt for a password. Without either, you're prompted only if the server asks for one and stdin is a terminal. |
| `--retries N` | After a lost connection, reconnect and run the interrupted query again up to N times, and start the dump over up to N times if that isn't possible (default 5). `0` uses a single connection and fails on the first loss. See [Lost connections](#lost-connections). |
| `--reconnect-timeout SECONDS` | How long to keep trying to reconnect after a loss (default 300). |

The other libpq variables work too (`PGSERVICE`, `PGSSLMODE`, `PGCONNECT_TIMEOUT`, …).
Unless one is configured, the connect timeout is 30 seconds, and TCP keepalives probe a quiet
connection after 60 seconds (`keepalives_idle`, `keepalives_interval` and `keepalives_count` in
the conninfo override this). A password given with
`--password` is visible to other local users in the process list, so prefer `PGPASSWORD` or
a password file.

There is no standard PostgreSQL environment variable for the schema, so it is only set with `-n`.

## Lost connections

A dump reads everything inside one snapshot, so the file is consistent even while the source
keeps changing. A plain reconnect would read the rest from a newer snapshot, and mixing the two
can produce a file that doesn't import (say, a child row read before its parent was deleted).
So intellidump opens a second connection that does nothing but hold the snapshot open. Then:

* **The working connection is lost** (network failure, a proxy or firewall dropping it, the
  backend being terminated): intellidump reconnects, joins the *same* snapshot
  (`SET TRANSACTION SNAPSHOT`) and runs the interrupted query again. It sees exactly the same
  rows, so nothing done so far is lost or repeated, and changes committed in the meantime don't
  leak in. Rows the interrupted query had already returned come again and are ignored.
* **The snapshot is lost too** (the server restarted or failed over, or both connections
  dropped): intellidump waits for the server and starts the dump over in a new snapshot. The
  output file is only written at the end, so nothing half-done is left behind.
* **The connection is lost while your query itself runs**: the dump starts over. Running your
  query again later could select different rows (it may use `now()`), so it is never re-run in
  the middle of a dump.

Each reconnect is logged as a warning. While waiting for the server, intellidump retries with a
growing delay (1, 2, 4, 8, 15, then every 30 seconds) for up to `--reconnect-timeout` seconds.
It gives up at once on errors that waiting won't fix, such as a rejected password. One query
can be re-run `--retries` times, and the dump can start over `--retries` times.

A new connection gets the settings your session had changed with `SET` (`TimeZone`,
`search_path`, `SET ROLE`, …), so the query behaves the same and runs with the same privileges.
Custom settings (`SET myapp.tenant = …`) can't be listed by PostgreSQL, so they aren't carried
over: a query that reads one with `current_setting()` fails if it has to be re-run, rather than
selecting something else. This only matters with `run_dump_on()`, since the CLI starts from a
fresh session.

If the second connection can't be opened (for example, the server is at `max_connections`), the
dump runs on one connection and a lost connection starts it over. On a hot standby, queries can
also be canceled by conflicts with recovery; that is handled the same way, but
`hot_standby_feedback = on` avoids it.

## Memory and disk use

Dumps can be larger than the machine's RAM. Rows never pile up in Python memory:

* Results stream from PostgreSQL `--fetch-size` rows at a time instead of being loaded whole.
* Each row goes straight into a temporary SQLite work database in `--work-dir`. Traversal
  bookkeeping lives there too: which rows still need their dependents or references looked up,
  and which key values were already looked up.
* Ordering, including the row-by-row ordering of self-referencing tables, runs as SQL inside that
  database.
* The `.sql` file is written by streaming rows back out of it.

Memory use is roughly a fixed amount (about 40 MB for Python and the driver) plus `--cache-size`.
It stays flat as the dump grows. In a test with a 64 MB cache:

| Rows | Output file | Peak process memory |
| ---: | ---: | ---: |
| 2,000,000 | 779 MB | 180 MB |
| 4,000,000 | 1,563 MB | 182 MB |

What you need is **disk**: the work directory needs about as much free space as the output file,
plus its indexes. Point `--work-dir` at a large, fast drive (not the drive the output goes to, if
you can help it). The work database is deleted when the run ends, including when it fails.
SQLite may put large sorts in the system temp directory (set `SQLITE_TMPDIR`, or `TMP` on
Windows, to move them).

A bigger `--cache-size` makes large dumps faster. A smaller one saves memory.

## How it works

1. A `REPEATABLE READ, READ ONLY` transaction is opened, so every lookup sees the same
   snapshot and nothing can be modified. The query is wrapped as a subquery, so only a single
   `SELECT` is accepted. A second connection holds the snapshot open, so a lost connection can
   be replaced without losing it (see [Lost connections](#lost-connections)). For that
   transaction only, output settings are fixed (`DateStyle`,
   `IntervalStyle`, `extra_float_digits`, `bytea_output`, …), so values come out the same
   whatever your session or role defaults are. `row_security` is turned off, as pg_dump does,
   so rows hidden by row-level security make the dump fail rather than silently go missing.
   Timeouts that would end a long dump (`statement_timeout`, `lock_timeout`,
   `idle_in_transaction_session_timeout`, and `transaction_timeout` on PostgreSQL 17+) are
   turned off for it, as pg_dump does.
2. The query's result metadata tells which table each output column comes from; that is the
   *root table*. Its rows are fetched by primary key.
3. Foreign keys inside the dumped schema(s) are read from `pg_catalog`, including composite
   keys, keys on unique columns, self-references, and partitioned tables.
4. Rows are collected breadth-first in both directions until nothing new turns up, into the
   work database described above. Each lookup is a batched, index-friendly join, and values
   already looked up are never requested again.
5. Tables are sorted topologically (referenced tables first). Tables in a reference cycle, like
   a self-referencing `parent_id` or two tables pointing at each other, are ordered row by row.
   If the data itself is circular, one row is inserted with the nullable foreign key set to NULL,
   and an `UPDATE` after the rows it points at restores the value.
6. Values are read as PostgreSQL's own text representation (`value::text`), so every type
   round-trips exactly: numeric, `timestamptz`, arrays, ranges, `jsonb`, `bytea`, enums,
   domains, extension types. Generated columns are left out, and `GENERATED ALWAYS` identity
   columns get `OVERRIDING SYSTEM VALUE`.
7. Every `INSERT` names its columns (`INSERT INTO "custom"."model" ("id", "name") VALUES …`),
   so the target's column order doesn't matter. Target columns that aren't in the source get
   their defaults.
8. The file is plain and line-safe: one statement per line (or per row with `--rows-per-insert`).
   Values with backslashes or control characters are written as `E'…'` literals with escapes,
   and names with control characters as `U&"…"`. So the file means the same whatever
   `standard_conforming_strings` is, and survives tools that rewrite line endings
   (git `autocrlf`, editors). Like pg_dump, the script sets `search_path` to empty and
   `xmloption` to `content`.

## Caveats

* **Cross-schema foreign keys are not followed.** If an exported row references a table outside
  the dumped schema(s), a warning is printed and those rows must already exist in the target.
  Pass `-n` more than once to dump several schemas together.
* **`ON CONFLICT DO NOTHING` skips a row on *any* unique conflict.** If the target already has a
  row with the same unique value (say an `email`) but a different id, the exported row is skipped,
  and rows referencing its id can then fail their foreign key check.
* **Tables without a primary key or unique constraint** have nothing to conflict on, so importing
  the same file twice duplicates their rows.
* **Triggers on the target fire during import**, just as with a pg_dump data-only restore.
  Because the script empties `search_path` (as pg_dump does), triggers or defaults on the target
  that call functions without a schema prefix can fail.
* **Row-level security**: dumping a table with RLS policies needs a role that bypasses them
  (superuser, the table owner, or `BYPASSRLS`). Otherwise the dump stops with an error.
* A data cycle made only of `NOT NULL` foreign keys can't be broken with an `UPDATE`. intellidump
  warns, and when `--transaction` is used it adds `SET CONSTRAINTS ALL DEFERRED`, which helps
  only if those constraints are `DEFERRABLE`.
* A single row must fit in memory: a 500 MB `bytea` value needs about that much RAM, briefly.
  Values whose text form is over about 1 GB (a `bytea` over about 500 MB) exceed the work
  database's limits.
* `money` values are written in the source's `lc_monetary` format. Import them where that
  setting is the same (this is also true of pg_dump).
* The query must select from tables. Views, and set-returning functions without `--table`,
  aren't supported.
* The source database should be UTF-8, or any encoding PostgreSQL can convert to UTF-8; a
  `SQL_ASCII` database holding non-UTF-8 bytes can't be dumped.
* Don't change table definitions (`ALTER TABLE`) while a dump runs. Row changes are fine: the
  dump reads one consistent snapshot.
* Statements use schema-qualified names (`"public"."prediction"`), so the file imports into the
  same schema name on the target unless you pass `--output-schema`. Every column the source
  table has (except generated ones) must also exist in the target table.

## Python API

```python
from intellidump import ConnectionOptions, DumpOptions, run_dump

result = run_dump(
    DumpOptions(
        query="SELECT * FROM prediction WHERE id = 42",
        output="prediction_42.sql",
        connection=ConnectionOptions(host="localhost", dbname="app"),
        on_conflict_do_nothing=True,
    )
)
print(result.total_rows, [(t.display_name, n) for t, n in result.counts])
```

`intellidump.dump.run_dump_on(conn, options)` accepts an existing psycopg connection instead.
The dump runs on that connection, and its settings are restored afterwards. Extra connections
(the snapshot holder, and replacements for lost connections) reuse its parameters and password;
pass `connect=` (a function returning a new connection) to open them differently.
`result.reconnects` and `result.restarts` say how often connections were lost.

## Development

```
pip install -e ".[dev]"
pytest                      # unit tests; integration tests are skipped
ruff check . && mypy
```

The integration tests need a PostgreSQL server and a role that can create databases, roles
and the `hstore`/`citext` extensions (a throwaway superuser is simplest):

```
docker run -d --rm --name idump-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17
INTELLIDUMP_TEST_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres pytest
```

They run on any supported server version. On older servers, test data a version can't hold
(generated columns before 12, numeric infinities and multiranges before 14) is left out
automatically; run them against `postgres:11` as well as a current version before a release.

What they cover:

* `tests/test_roundtrip.py`: every export is **re-imported into a separate, empty database**
  (`intellidump_rt_target`) with all constraints on, then compared value by value (`row::text`).
  The expected rows come from an independent oracle that follows each foreign key with a join in
  PostgreSQL. Cases include 50+ data types with edge values, foreign keys on 20+ key types,
  hostile identifiers and values, hostile session settings on both sides, partitioning,
  inheritance, cycles, concurrent writes during a dump, and failure cleanup.
* `tests/test_resilience.py`: the dump's connection is terminated (`pg_terminate_backend`) at
  chosen points, mid-query or between queries, with committed changes in between. Each dump must
  re-import and match the oracle for the original snapshot, or for the new one when it had to
  start over.
* `tests/test_fuzz.py`: random schemas (assorted key types, self-references, cycles) with random
  data and options, each checked the same way. In half the cases the connection is also killed
  one to three times along the way. Use `INTELLIDUMP_FUZZ_SEEDS=2000` for a longer run.

To replay every dump through the real `psql` client instead of psycopg, set
`INTELLIDUMP_TEST_PSQL`, e.g.
`INTELLIDUMP_TEST_PSQL="docker exec -i idump-pg psql -U postgres -d {dbname}"`.
