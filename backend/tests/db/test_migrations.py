"""Alembic migrations: head == models, legacy Phase-1 databases upgrade in place, downgrade works."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, event, inspect, text

from papermind.db import migrate as migrate_mod
from papermind.db import models  # noqa: F401
from papermind.db.base import Base, Database
from papermind.db.migrate import BASELINE, alembic_config, current_revision, upgrade_to_head
from papermind.db.models import BookRow, LedgerEntryRow
from tests.conftest import T0


def test_fresh_database_head_matches_models(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    db.init_schema()
    assert current_revision(db.engine) == "0003_phase3"
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
    assert current_revision(db.engine) == "0003_phase3"
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


# ------------------------------------------------------------------ the Phase-3 wedge, guarded
#
# A boot once got part-way through 0002 -> 0003 and was interrupted. pysqlite never opens a
# transaction for DDL, so the tables it had already created stayed behind while alembic_version
# never moved, and every boot after that died on "table llm_cache already exists". These three
# tests hold the invariants that make that impossible, for Phase 4's tables and anything later.


def test_create_all_refuses_a_file_database(tmp_path: Path) -> None:
    """Only Alembic may create tables in a real database — nothing else, ever."""
    db = Database(f"sqlite:///{tmp_path / 'real.db'}")
    with pytest.raises(RuntimeError, match="only Alembic may create tables"):
        db.create_all()
    assert inspect(db.engine).get_table_names() == []  # and it created nothing on the way out

    db.init_schema()  # the supported path still works
    assert current_revision(db.engine) == "0003_phase3"


def test_an_interrupted_migration_leaves_nothing_behind(tmp_path: Path) -> None:
    """Migrations are all-or-nothing, so an interrupted boot can always just retry.

    Kills the upgrade at the statement the real incident died on — the first NOT NULL column
    added to `signals` — by which point 0003 has already created two tables and one column.
    """
    url = f"sqlite:///{tmp_path / 'interrupted.db'}"
    db = Database(url)
    cfg = alembic_config(url)
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0002_phase2")

    class Killed(Exception):
        pass

    real_engine = migrate_mod._migration_engine

    def kill_at_reasoning(u: str) -> Engine:
        eng = real_engine(u)

        @event.listens_for(eng, "before_cursor_execute")
        def _kill(conn: Any, cur: Any, stmt: str, params: Any, ctx: Any, many: bool) -> None:
            if "reasoning" in stmt:
                raise Killed(stmt)

        return eng

    migrate_mod._migration_engine = kill_at_reasoning
    try:
        with pytest.raises(Killed):
            upgrade_to_head(db.engine)
    finally:
        migrate_mod._migration_engine = real_engine

    # Nothing from the half-run migration survived: no new tables, no new column, same revision.
    tables = set(inspect(db.engine).get_table_names())
    assert "llm_cache" not in tables and "llm_calls" not in tables
    assert "confidence" not in {c["name"] for c in inspect(db.engine).get_columns("signals")}
    assert current_revision(db.engine) == "0002_phase2"

    # ...so the next boot just migrates, which is the whole point.
    upgrade_to_head(db.engine)
    assert current_revision(db.engine) == "0003_phase3"
    with db.engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diffs == []


def test_the_suite_cannot_open_the_real_database() -> None:
    """The `_no_real_database` guard in conftest is live for every test in this run.

    `db_url` defaults to the relative `sqlite:///papermind.db`, so without this guard a test
    that forgets a tmp path silently migrates the developer's own database instead of failing.
    """
    db = Database("sqlite:///papermind.db")
    with pytest.raises(Exception, match="outside pytest's tmp dir"):
        db.init_schema()
