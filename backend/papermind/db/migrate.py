"""Schema management via Alembic.

- File databases are always brought to `head` with Alembic on startup.
- Databases created by Phase 1 (via create_all, no alembic_version table) are stamped at the
  Phase-1 baseline first, then upgraded — existing journal data is preserved.
- In-memory databases (tests, backtest scratch DBs) use create_all; a test asserts that
  create_all and the migrations produce the same schema.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
BASELINE = "0001_phase1"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade_to_head(engine: Engine) -> None:
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and "trades" in tables:
            log.warning("legacy database without alembic_version: stamping %s before upgrading", BASELINE)
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            return None
        row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").first()
        return str(row[0]) if row else None
