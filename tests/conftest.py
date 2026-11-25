from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import FakeSource, catalog, col, fk, r, table
from intellidump.models import Catalog, Table
from intellidump.store import RowStore


@dataclass
class Example:
    """model <- prediction <- entity (self-referencing) <- action -> app_user."""

    catalog: Catalog
    source: FakeSource
    model: Table
    app_user: Table
    prediction: Table
    entity: Table
    action: Table


@pytest.fixture
def example() -> Example:
    model = table("model", col("id"), col("name", type_name="text", category="S"))
    app_user = table("app_user", col("id"), col("email", type_name="text", category="S"))
    prediction = table(
        "prediction",
        col("id"),
        col("model_id", not_null=True),
        col("created", type_name="timestamp with time zone", category="D"),
    )
    entity = table("entity", col("id"), col("prediction_id", not_null=True), col("parent_id"))
    action = table("action", col("id"), col("entity_id", not_null=True), col("user_id"))
    cat = catalog(
        [model, app_user, prediction, entity, action],
        [
            fk("prediction_model_fk", prediction, ["model_id"], model),
            fk("entity_prediction_fk", entity, ["prediction_id"], prediction),
            fk("entity_parent_fk", entity, ["parent_id"], entity),
            fk("action_entity_fk", action, ["entity_id"], entity),
            fk("action_user_fk", action, ["user_id"], app_user),
        ],
    )
    source = FakeSource(
        {
            model: [r(1, "m1"), r(2, "m2")],
            app_user: [r(1, "a@x"), r(2, "b@x"), r(3, "c@x")],
            prediction: [
                r(1, 1, "2026-11-20 00:00:00+00"),
                r(2, 1, "2026-11-01 00:00:00+00"),  # same model, not selected
                r(3, 2, "2026-11-21 00:00:00+00"),
            ],
            entity: [
                # 12 is the parent of 10; ids out of order on purpose.
                r(10, 1, 12),
                r(11, 1, None),
                r(12, 1, None),
                r(13, 1, 10),
                r(20, 2, None),  # belongs to the unselected prediction
                r(30, 3, None),
            ],
            action: [
                r(100, 10, 1),
                r(101, 13, 2),
                r(102, 20, 3),  # under the unselected prediction
                r(103, 11, None),
            ],
        }
    )
    return Example(cat, source, model, app_user, prediction, entity, action)


@pytest.fixture
def store(tmp_path):
    with RowStore(work_dir=tmp_path) as s:
        yield s


@pytest.fixture(scope="session")
def rt_target():
    """Create the empty target database for round-trip tests, once."""
    import rt

    if not rt.DSN:
        pytest.skip("INTELLIDUMP_TEST_DSN is not set")
    rt.create_target_database()
    yield
    rt.drop_target_database()


@pytest.fixture
def env(rt_target, tmp_path):
    """Round-trip environment: source and target connections plus a scratch dir."""
    import psycopg

    import rt

    (tmp_path / "work").mkdir()
    with (
        psycopg.connect(rt.DSN, autocommit=True) as src,
        psycopg.connect(rt.target_dsn(), autocommit=True) as dst,
    ):
        for conn in (src, dst):
            for ext in ("hstore", "citext"):
                conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext} SCHEMA public")
        e = rt.Env(src=src, dst=dst, schema="rt", tmp=tmp_path)
        yield e
        for conn in (src, dst):
            conn.execute("DROP SCHEMA IF EXISTS rt CASCADE")
