"""Alembic migrations: head == models, legacy Phase-1 databases upgrade in place, downgrade works."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from papermind.db import models  # noqa: F401
from papermind.db.base import Base, Database
from papermind.db.migrate import BASELINE, alembic_config, current_revision, upgrade_to_head
from papermind.db.models import BookRow, LedgerEntryRow
from tests.conftest import T0


def test_fresh_database_head_matches_models(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    db.init_schema()
    assert current_revision(db.engine) == "0002_phase2"
    with db.engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diffs == [], f"models and migrations differ: {diffs}"
    db.init_schema()  # idempotent


def test_legacy_phase1_database_is_stamped_and_upgraded(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    db = Database(url)
    cfg = alembic_config(url)
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, BASELINE)  # exactly the Phase-1 schema
        conn.execute(text("DROP TABLE alembic_version"))  # Phase 1 used create_all: no version table
    with db.session() as s:
        s.add(
            BookRow(
                id="b",
                market="crypto",
                segment="futures",
                style="intraday",
                currency="USDT",
                mode="manual",
                created_at=T0,
            )
        )
        s.flush()
        s.add(LedgerEntryRow(book_id="b", account="main", ts=T0, kind="deposit", amount=Decimal("1000"), note=""))
    assert current_revision(db.engine) is None

    upgrade_to_head(db.engine)
    assert current_revision(db.engine) == "0002_phase2"
    tables = set(inspect(db.engine).get_table_names())
    assert {"signals", "backtest_runs", "trades"} <= tables
    with db.session() as s:
        assert s.query(LedgerEntryRow).one().amount == Decimal("1000")  # data preserved


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'down.db'}"
    db = Database(url)
    db.init_schema()
    cfg = alembic_config(url)
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    assert set(inspect(db.engine).get_table_names()) <= {"alembic_version"}
    db.init_schema()
    assert "signals" in inspect(db.engine).get_table_names()


def test_in_memory_uses_create_all() -> None:
    db = Database("sqlite://")
    assert db.in_memory
    db.init_schema()
    assert "signals" in inspect(db.engine).get_table_names()
    assert current_revision(db.engine) is None
