"""llm_calls: record how long a call waited for its rate-limit slot, separately from the provider

A failed call used to store `latency_ms = 0`, so the only calls with any timing were the ones
that worked — which made "the LLM is slow" and "the LLM was rate-limited for 50s" look identical
in the call log. `queue_wait_ms` splits our own queueing off the provider's latency so the two can
never be confused again; `latency_ms` now covers failures too.

Revision ID: 0004_llm_timing
Revises: 0003_phase3
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_llm_timing"
down_revision: str | None = "0003_phase3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default: rows written before this migration have no value for a NOT NULL column
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("queue_wait_ms", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_column("queue_wait_ms")
