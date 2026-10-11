"""Schema management via Alembic.

- File databases are always brought to `head` with Alembic on startup.
- Databases created by Phase 1 (via create_all, no alembic_version table) are stamped at the
  Phase-1 baseline first, then upgraded — existing journal data is preserved.
- In-memory databases (tests, backtest scratch DBs) use create_all; a test asserts that
  create_all and the migrations produce the same schema.
- Migrations run with DDL inside a real transaction, so an interrupted one leaves NOTHING
  behind — see `_migration_engine` for why that needs saying on SQLite.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event, inspect

from papermind.db.base import make_engine

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
BASELINE = "0001_phase1"


def _ini_escape(value: str) -> str:
    """Escape a literal value for Alembic's ConfigParser (BasicInterpolation eats bare `%`).

    SQLAlchemy percent-encodes Windows paths when rendering a URL
    (``sqlite:///C%3A%5C...``), and any path may contain ``%``. ConfigParser reads
    ``%%`` back as a single ``%``, so escaping round-trips exactly.
    """
    return value.replace("%", "%%")


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _ini_escape(str(MIGRATIONS_DIR)))
    cfg.set_main_option("sqlalchemy.url", _ini_escape(url))
    return cfg


def _migration_engine(url: str) -> Engine:
    """A private engine for the duration of one migration, with DDL inside the transaction.

    SQLite is perfectly transactional for DDL; pysqlite is what is not. The driver only opens a
    transaction implicitly before DML, never before CREATE/ALTER/DROP, so by default every DDL
    statement in a migration commits the instant it runs — `engine.begin()` cannot roll it back.
    Interrupt a boot mid-migration (a `uvicorn --reload` restart, a Ctrl+C) and the tables it
    already created stay behind while `alembic_version` never moves, so every later boot dies on
    "table ... already exists" with the database wedged between two revisions. That is exactly
    how the Phase-2 -> Phase-3 upgrade broke once.

    Opting out of pysqlite's implicit transactions and emitting BEGIN ourselves (the recipe from
    SQLAlchemy's pysqlite notes) puts the DDL back under the transaction, so a migration is
    all-or-nothing again and an interrupted boot simply retries from the last good revision.
    """
    engine = make_engine(url)
    if engine.dialect.name != "sqlite":
        return engine  # every other backend we support already has transactional DDL

    @event.listens_for(engine, "connect")
    def _no_implicit_transactions(dbapi_conn: object, _rec: object) -> None:
        dbapi_conn.isolation_level = None  # type: ignore[attr-defined]

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


def upgrade_to_head(engine: Engine) -> None:
    """Bring a file database to head. Atomic: either it reaches head or nothing changed."""
    url = str(engine.url)
    cfg = alembic_config(url)
    migration_engine = _migration_engine(url)
    try:
        with migration_engine.begin() as conn:
            cfg.attributes["connection"] = conn
            tables = set(inspect(conn).get_table_names())
            if "alembic_version" not in tables and "trades" in tables:
                log.warning("legacy database without alembic_version: stamping %s before upgrading", BASELINE)
                command.stamp(cfg, BASELINE)
            command.upgrade(cfg, "head")
    finally:
        migration_engine.dispose()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            return None
        row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").first()
        return str(row[0]) if row else None
