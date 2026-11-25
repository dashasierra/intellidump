from __future__ import annotations

from helpers import FakeSource, catalog, col, fk, r, table
from intellidump.collector import Collector
from intellidump.ordering import Insert, Update, build_plan, iter_plan, strongly_connected


def plan_events(cat, src, store, seed_table, pred=lambda row: True):
    c = Collector(cat, src, store)
    c.add_seed(seed_table, src.seed(seed_table, pred))
    c.run()
    plan = build_plan(cat, store)
    return plan, list(iter_plan(plan, store))


def inserts(events):
    return [e for e in events if isinstance(e, Insert)]


def insert_sequence(events):
    return [(e.table.name, e.row[0]) for e in inserts(events)]


def test_parents_are_written_before_children(example, store):
    plan, events = plan_events(
        example.catalog, example.source, store, example.prediction, lambda r: r[0] == "1"
    )
    order = [t.name for t in plan.table_order]
    assert order.index("model") < order.index("prediction")
    assert order.index("prediction") < order.index("entity") < order.index("action")
    assert order.index("app_user") < order.index("action")
    assert not plan.warnings
    # Events come out grouped by table in that same order.
    names = [e.table.name for e in inserts(events)]
    assert sorted(set(names), key=names.index) == order


def test_self_referencing_rows_are_ordered_parent_first(example, store):
    plan, events = plan_events(
        example.catalog, example.source, store, example.prediction, lambda r: r[0] == "1"
    )
    entities = [rid for name, rid in insert_sequence(events) if name == "entity"]
    assert sorted(entities, key=int) == ["10", "11", "12", "13"]
    assert entities.index("12") < entities.index("10") < entities.index("13")
    assert not [e for e in events if isinstance(e, Update)]
    assert plan.update_count == 0


def test_rows_sorted_numerically_within_table(store):
    t = table("t", col("id"))
    cat = catalog([t], [])
    src = FakeSource({t: [r(10), r(9), r(100), r(1)]})
    _, events = plan_events(cat, src, store, t)
    assert [e.row[0] for e in events] == ["1", "9", "10", "100"]


def test_text_keys_sorted_and_values_kept_verbatim(store):
    t = table(
        "t",
        col("code", type_name="text", category="S"),
        col("amount", type_name="numeric"),
        pk=("code",),
    )
    cat = catalog([t], [])
    src = FakeSource({t: [r("b", "1.50"), r("a", "007"), r("c", None)]})
    _, events = plan_events(cat, src, store, t)
    # numeric is not an integer type: its text form must survive untouched.
    assert [e.row for e in events] == [("a", "007"), ("b", "1.50"), ("c", None)]


def test_cycle_between_tables_is_broken_with_nullable_fk(store):
    # team.captain_id -> member (nullable); member.team_id -> team (NOT NULL).
    team = table("team", col("id"), col("captain_id"))
    member = table("member", col("id"), col("team_id", not_null=True))
    cat = catalog(
        [team, member],
        [
            fk("team_captain", team, ["captain_id"], member),
            fk("member_team", member, ["team_id"], team),
        ],
    )
    src = FakeSource({team: [r(1, 5)], member: [r(5, 1), r(6, 1)]})
    plan, events = plan_events(cat, src, store, team)

    assert events[0] == Insert(team, ("1", None))  # captain deferred
    assert insert_sequence(events) == [("team", "1"), ("member", "5"), ("member", "6")]
    updates = [e for e in events if isinstance(e, Update)]
    assert updates == [Update(team, where=[("id", "1")], assignments=[("captain_id", "5")])]
    assert isinstance(events[-1], Update)
    assert plan.update_count == 1
    assert not plan.warnings


def test_many_independent_cycles(store):
    team = table("team", col("id"), col("captain_id"))
    member = table("member", col("id"), col("team_id", not_null=True))
    cat = catalog(
        [team, member],
        [
            fk("team_captain", team, ["captain_id"], member),
            fk("member_team", member, ["team_id"], team),
        ],
    )
    n = 50
    src = FakeSource(
        {
            team: [r(i, 1000 + i) for i in range(n)],
            member: [r(1000 + i, i) for i in range(n)],
        }
    )
    plan, events = plan_events(cat, src, store, team)
    rows = inserts(events)
    assert len(rows) == 2 * n
    placed: set[tuple[str, str]] = set()
    for e in rows:
        if e.table is team and e.row[1] is not None:
            assert ("member", e.row[1]) in placed
        if e.table is member:
            assert ("team", e.row[1]) in placed
        placed.add((e.table.name, e.row[0]))
    assert plan.update_count == n


def test_self_reference_data_cycle(store):
    node = table("node", col("id"), col("next_id"))
    cat = catalog([node], [fk("node_next", node, ["next_id"], node)])
    src = FakeSource({node: [r(1, 2), r(2, 3), r(3, 1), r(4, 4)]})
    _, events = plan_events(cat, src, store, node)
    rows = [e.row for e in inserts(events)]
    assert len(rows) == 4
    assert ("4", "4") in rows  # a row pointing at itself needs no special handling
    assert len([e for e in events if isinstance(e, Update)]) == 1
    seen: set[str] = set()
    for row in rows:
        if row[1] is not None and row[1] != row[0]:
            assert row[1] in seen
        seen.add(row[0])


def test_deep_chain(store):
    node = table("node", col("id"), col("parent_id"))
    cat = catalog([node], [fk("node_parent", node, ["parent_id"], node)])
    depth = 300
    # Inserted in reverse so key order is the opposite of dependency order.
    src = FakeSource({node: [r(i, i - 1 if i else None) for i in reversed(range(depth))]})
    _, events = plan_events(cat, src, store, node)
    assert [e.row[0] for e in events] == [str(i) for i in range(depth)]


def test_not_null_data_cycle_warns(store):
    a = table("a", col("id"), col("b_id", not_null=True))
    b = table("b", col("id"), col("a_id", not_null=True))
    cat = catalog([a, b], [fk("a_b", a, ["b_id"], b), fk("b_a", b, ["a_id"], a)])
    src = FakeSource({a: [r(1, 1)], b: [r(1, 1)]})
    plan, events = plan_events(cat, src, store, a)
    assert plan.warnings
    assert len(inserts(events)) == 2


def test_strongly_connected_reverse_topological():
    graph = {"a": ["b"], "b": ["c"], "c": ["b", "d"], "d": []}
    comps = strongly_connected(graph, lambda n: graph[n])
    assert [sorted(c) for c in comps] == [["d"], ["b", "c"], ["a"]]
