"""phase3: LLM analyst calls + response cache, co-pilot cards, shadow-trade links

Reviewed by hand after autogenerate: every NOT NULL column added to the existing `signals`
table needs a server_default, otherwise the rows already in a Phase-2 database have no value
for it and the ALTER fails.

Revision ID: 0003_phase3
Revises: 0002_phase2
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import papermind.db.base

revision: str = "0003_phase3"
down_revision: str | None = "0002_phase2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_cache",
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("created_at", papermind.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("prompt_hash"),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("ts", papermind.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=40), nullable=True),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_reason", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("thought_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.create_index("ix_llm_calls_book_ts", ["book_id", "ts"], unique=False)
        batch_op.create_index("ix_llm_calls_ts", ["ts"], unique=False)

    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("confidence", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reasoning", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("risks_noted", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("lessons_applied", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("adjustments", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(sa.Column("prompt_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("llm_call_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("card_status", sa.String(length=16), nullable=False, server_default="none"))
        batch_op.add_column(sa.Column("card_expires_at", papermind.db.base.UTCDateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("card_decided_at", papermind.db.base.UTCDateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("raw_trade_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("rejected_trade_id", sa.String(length=40), nullable=True))
        batch_op.create_index("ix_signals_card", ["book_id", "card_status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.drop_index("ix_signals_card")
        for col in (
            "rejected_trade_id",
            "raw_trade_id",
            "card_decided_at",
            "card_expires_at",
            "card_status",
            "llm_call_id",
            "prompt_version",
            "adjustments",
            "lessons_applied",
            "risks_noted",
            "reasoning",
            "confidence",
        ):
            batch_op.drop_column(col)

    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_calls_ts")
        batch_op.drop_index("ix_llm_calls_book_ts")
    op.drop_table("llm_calls")
    op.drop_table("llm_cache")
