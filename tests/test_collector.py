from __future__ import annotations

import pytest

from helpers import FakeSource, catalog, col, fk, r, table
from intellidump.collector import Collector
from intellidump.errors import IntellidumpError
from intellidump.store import RowStore


def ids(store, tbl) -> list[int]:
    return sorted(int(k[0]) for k in store.iter_keys(tbl))


@pytest.fixture
def seed_prediction_1(example, store):
    def run(**kwargs):
        collector = Collector(example.catalog, example.source, store, **kwargs)
        collector.add_seed(
            example.prediction, example.source.seed(example.prediction, lambda row: row[0] == "1")
        )
        collector.run()
        return store

    return run


def test_collects_descendants_and_their_parents_only(example, seed_prediction_1):
    got = seed_prediction_1()
    assert ids(got, example.prediction) == [1]
    assert ids(got, example.entity) == [10, 11, 12, 13]
    assert ids(got, example.action) == [100, 101, 103]
    assert ids(got, example.app_user) == [1, 2]
    assert ids(got, example.model) == [1]


def test_parent_rows_do_not_pull_in_their_other_children(example, seed_prediction_1):
    got = seed_prediction_1()
    # model 1 is a parent of predictions 1 and 2, but only 1 was selected.
    assert 2 not in ids(got, example.prediction)
    assert 20 not in ids(got, example.entity)


def test_no_parents(example, seed_prediction_1):
    got = seed_prediction_1(follow_parents=False)
    assert ids(got, example.model) == []
    assert ids(got, example.app_user) == []
    assert ids(got, example.action) == [100, 101, 103]


def test_no_children(example, seed_prediction_1):
    got = seed_prediction_1(follow_children=False)
    assert ids(got, example.entity) == []
    assert ids(got, example.model) == [1]


def test_excluded_table_stops_traversal(example, seed_prediction_1):
    got = seed_prediction_1(excluded={example.action.oid})
    assert ids(got, example.action) == []
    assert ids(got, example.app_user) == []
    assert ids(got, example.entity) == [10, 11, 12, 13]


def test_max_rows(seed_prediction_1):
    with pytest.raises(IntellidumpError, match="--max-rows"):
        seed_prediction_1(max_rows=5)


def test_no_empty_or_repeated_lookups(example, seed_prediction_1):
    seed_prediction_1()
    assert all(n > 0 for _, _, n in example.source.calls)
    # Values already fetched for a (table, columns) pair are never requested again.
    seen: dict[tuple[str, tuple[str, ...]], set[tuple[str, ...]]] = {}
    for name, cols, values in example.source.requests:
        previous = seen.setdefault((name, cols), set())
        assert not previous & values
        previous |= values


def test_row_first_seen_as_parent_is_expanded_when_reached_as_descendant(tmp_path):
    # item -> owner (parent); note -> item and note -> owner; seed item 1.
    # owner 7 enters as item 1's parent. Its children must not be expanded...
    owner = table("owner", col("id"))
    item = table("item", col("id"), col("owner_id"))
    note = table("note", col("id"), col("owner_id"))
    cat = catalog(
        [owner, item, note],
        [fk("item_owner", item, ["owner_id"], owner), fk("note_owner", note, ["owner_id"], owner)],
    )
    src = FakeSource({owner: [r(7)], item: [r(1, 7), r(2, 7)], note: [r(50, 7)]})
    got = RowStore(work_dir=tmp_path)
    c = Collector(cat, src, got)
    c.add_seed(item, src.seed(item, lambda row: row[0] == "1"))
    c.run()
    assert ids(got, owner) == [7]
    assert ids(got, item) == [1]
    assert ids(got, note) == []

    # ...but if the owner itself is selected, its notes and items come along.
    got.close()
    got = RowStore(work_dir=tmp_path)
    c = Collector(cat, src, got)
    c.add_seed(item, src.seed(item, lambda row: row[0] == "1"))
    c.add_seed(owner, src.seed(owner, lambda row: True))
    c.run()
    assert ids(got, note) == [50]
    assert ids(got, item) == [1, 2]
    got.close()


def test_null_foreign_keys_reference_nothing(example, seed_prediction_1):
    got = seed_prediction_1()
    # action 103 has user_id NULL: no lookup for it, and no user 3.
    assert 3 not in ids(got, example.app_user)


def test_tables_without_a_key_keep_duplicate_rows(store):
    parent = table("parent", col("id"))
    log = table("log", col("parent_id"), col("msg", type_name="text", category="S"), pk=())
    cat = catalog([parent, log], [fk("log_parent", log, ["parent_id"], parent)])
    src = FakeSource({parent: [r(1), r(2)], log: [r(1, "a"), r(1, "a"), r(2, "b")]})
    c = Collector(cat, src, store)
    c.add_seed(parent, src.seed(parent, lambda row: row[0] == "1"))
    c.run()
    assert list(store.iter_rows(log)) == [("1", "a"), ("1", "a")]


def test_work_database_is_removed_on_close(tmp_path):
    with RowStore(work_dir=tmp_path) as s:
        assert list(tmp_path.iterdir())
    assert not list(tmp_path.iterdir())
    assert s.path
