"""Randomised schemas and data: every dump must re-import into an empty database
and contain exactly the rows the oracle says it should.

Each seed builds 2-6 tables with assorted key types (integer, text, composite,
citext and numeric keys, or no key at all), random foreign keys between them
(including self-references and cycles through nullable columns), and random
data in which references are written differently from the keys they point at
where the type allows it ('ABC' for citext 'abc', 1.5 for numeric 1.50).
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field

import psycopg
import pytest

import rt
from rt import ConnectionKiller, needs_db

pytestmark = [pytest.mark.integration, needs_db]

#: Number of random cases; raise it for a longer soak, e.g. INTELLIDUMP_FUZZ_SEEDS=2000.
SEEDS = range(int(os.environ.get("INTELLIDUMP_FUZZ_SEEDS", "80")))

TEXTS = [
    "a",
    "b",
    "it's",
    'dq"x',
    "back\\slash",
    "new\nline",
    "tab\tx",
    "🙂",
    "ünï",
    "%s",
    "{}",
    "--x",
    "; x",
    "sp ace",
    "",
    "Z",
    "zz",
    "0",
    "10",
    "9",
]
WORDS = ["alpha", "beta", "gamma", "delta", "eps", "zeta", "eta", "theta", "iota", "kappa"]


@dataclass
class T:
    name: str
    kind: str  # int | text | comp | ci | num | none
    rows: list[dict[str, str]] = field(default_factory=list)
    fks: list[tuple[str, T, bool]] = field(default_factory=list)  # (prefix, parent, not_null)

    @property
    def key_cols(self) -> list[str]:
        return {
            "int": ["id"],
            "text": ["id"],
            "ci": ["id"],
            "num": ["id"],
            "comp": ["a", "b"],
            "none": [],
        }[self.kind]

    def key_types(self) -> list[str]:
        return {
            "int": ["int"],
            "text": ["text"],
            "ci": ["public.citext"],
            "num": ["numeric(8,2)"],
            "comp": ["int", "text"],
            "none": [],
        }[self.kind]

    def ref_types(self) -> list[str]:
        # Referencing columns: plain numeric so 1.5 can reference 1.50.
        return {
            "int": ["int"],
            "text": ["text"],
            "ci": ["public.citext"],
            "num": ["numeric"],
            "comp": ["int", "text"],
            "none": [],
        }[self.kind]


def lit(v: str | None) -> str:
    if v is None:
        return "NULL"
    return (
        "E'"
        + v.replace("\\", "\\\\").replace("'", "''").replace("\n", "\\n").replace("\t", "\\t")
        + "'"
    )


def build(rng: random.Random) -> tuple[str, str, list[T]]:
    n = rng.randint(2, 6)
    tables = [
        T(f"t{i}", rng.choice(["int", "int", "text", "comp", "ci", "num", "none"]))
        for i in range(n)
    ]
    keyed = [t for t in tables if t.kind != "none"]
    if not keyed:
        tables[0].kind = "int"
        keyed = [tables[0]]
    for i, t in enumerate(tables):
        for k in range(rng.randint(0, 3)):
            parent = rng.choice(keyed)
            earlier = tables.index(parent) < i
            t.fks.append((f"f{k}", parent, earlier and rng.random() < 0.4))

    # rows, with keys inserted in random order
    for t in tables:
        count = rng.randint(1, 25)
        if t.kind == "int":
            keys = [[str(v)] for v in rng.sample(range(1, 500), count)]
        elif t.kind == "text":
            keys = [[v] for v in rng.sample(TEXTS, min(count, len(TEXTS)))]
        elif t.kind == "ci":
            keys = [[v] for v in rng.sample(WORDS, min(count, len(WORDS)))]
        elif t.kind == "num":
            keys = [[f"{v / 4:.2f}"] for v in rng.sample(range(-40, 400), count)]
        elif t.kind == "comp":
            pairs = {(rng.randint(1, 5), rng.choice(TEXTS)) for _ in range(count)}
            keys = [[str(a), b] for a, b in pairs]
        else:
            keys = [[] for _ in range(count)]
        for key in keys:
            row = dict(zip(t.key_cols, key, strict=True))
            row["payload"] = rng.choice(TEXTS)
            t.rows.append(row)

    def ref(parent: T) -> list[str]:
        target = rng.choice(parent.rows)
        key = [target[c] for c in parent.key_cols]
        if parent.kind == "ci":  # same value to citext, different text
            key = ["".join(ch.upper() if rng.random() < 0.5 else ch for ch in key[0])]
        if parent.kind == "num":  # same number, different scale
            key = [rng.choice([key[0], key[0].rstrip("0").rstrip("."), key[0] + "000"]) or "0"]
        return key

    ddl, inserts, updates = [], [], []
    for t in tables:
        cols = [f"{c} {typ} NOT NULL" for c, typ in zip(t.key_cols, t.key_types(), strict=True)]
        cols.append("payload text")
        for prefix, parent, not_null in t.fks:
            for c, typ in zip(parent.key_cols, parent.ref_types(), strict=True):
                cols.append(f"{prefix}_{c} {typ}{' NOT NULL' if not_null else ''}")
        if t.key_cols:
            cols.append(f"PRIMARY KEY ({', '.join(t.key_cols)})")
        ddl.append(f"CREATE TABLE {t.name} ({', '.join(cols)});")
    for t in tables:
        for prefix, parent, _ in t.fks:
            fcols = ", ".join(f"{prefix}_{c}" for c in parent.key_cols)
            ddl.append(
                f"ALTER TABLE {t.name} ADD FOREIGN KEY ({fcols}) "
                f"REFERENCES {parent.name} ({', '.join(parent.key_cols)});"
            )
    for t in tables:
        for row in t.rows:
            names, vals = list(row), [lit(v) for v in row.values()]
            for prefix, parent, not_null in t.fks:
                if not_null:  # parent is an earlier table: its rows already exist
                    for c, v in zip(parent.key_cols, ref(parent), strict=True):
                        names.append(f"{prefix}_{c}")
                        vals.append(lit(v))
            inserts.append(f"INSERT INTO {t.name} ({', '.join(names)}) VALUES ({', '.join(vals)});")
    for t in tables:
        if not t.key_cols:
            continue
        for row in t.rows:
            sets = []
            for prefix, parent, not_null in t.fks:
                if not_null or rng.random() < 0.3:
                    continue
                for c, v in zip(parent.key_cols, ref(parent), strict=True):
                    sets.append(f"{prefix}_{c} = {lit(v)}")
            if sets:
                where = " AND ".join(f"{c} = {lit(row[c])}" for c in t.key_cols)
                updates.append(f"UPDATE {t.name} SET {', '.join(sets)} WHERE {where};")
    # Rows without a key get references directly (nothing can point at them).
    for t in tables:
        if t.key_cols:
            continue
        for prefix, parent, not_null in t.fks:
            if not not_null:
                cols = [f"{prefix}_{c}" for c in parent.key_cols]
                sets = ", ".join(f"{c} = {lit(v)}" for c, v in zip(cols, ref(parent), strict=True))
                payload = lit(rng.choice(t.rows)["payload"])
                updates.append(f"UPDATE {t.name} SET {sets} WHERE payload = {payload};")
    return "\n".join(ddl), "\n".join(inserts + updates), tables


@pytest.mark.parametrize("seed", SEEDS)
def test_random_schema_round_trips(env, seed, monkeypatch):
    rng = random.Random(seed)
    ddl, data, tables = build(rng)
    env.setup(ddl, data)

    roots = [t for t in tables if t.key_cols]
    root = rng.choice(roots)
    where = rng.choice(
        [
            "true",
            f"payload IN ({lit(rng.choice(TEXTS))}, {lit(rng.choice(TEXTS))})",
            f"{root.key_cols[0]}::text < {lit(rng.choice(TEXTS + WORDS))}",
        ]
    )
    opts = {
        "rows_per_insert": rng.choice([1, 1, 3, 100]),
        "on_conflict_do_nothing": rng.random() < 0.5,
        "transaction": rng.random() < 0.5,
        "follow_children": rng.random() < 0.85,
        "batch_size": rng.choice([1, 2, 1000]),
        "fetch_size": rng.choice([1, 1000]),
    }
    no_key_rows = any(not t.key_cols for t in tables)
    # Half the cases lose their connection one to three times along the way.
    chaos = rng.random() < 0.5
    kills = set(rng.sample(range(1, 13), rng.randint(1, 3)))
    after_rows, before = rng.choice([0, 1, 4]), rng.random() < 0.3
    with psycopg.connect(rt.DSN, autocommit=True) as conn:
        if chaos:
            env.dump_conn = conn
            ConnectionKiller(monkeypatch, at=kills, after_rows=after_rows, before=before)
        env.roundtrip(
            f"rt.{root.name}",
            where,
            replay_twice=opts["on_conflict_do_nothing"] and not no_key_rows,
            crlf=rng.random() < 0.5,
            **opts,
        )
