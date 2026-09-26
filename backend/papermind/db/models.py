"""Database tables. Money = DecimalText, time = UTCDateTime (tz-aware UTC)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from papermind.db.base import Base, DecimalText, UTCDateTime


class InstrumentRow(Base):
    __tablename__ = "instruments"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    segment: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    underlying: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(8))
    quote_ccy: Mapped[str] = mapped_column(String(8))
    settle_ccy: Mapped[str] = mapped_column(String(8))
    contract_size: Mapped[Decimal] = mapped_column(DecimalText)
    lot_size: Mapped[Decimal] = mapped_column(DecimalText)
    tick_size: Mapped[Decimal] = mapped_column(DecimalText)
    qty_step: Mapped[Decimal] = mapped_column(DecimalText)
    min_qty: Mapped[Decimal] = mapped_column(DecimalText)
    expiry: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    broker_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    refreshed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class BookRow(Base):
    __tablename__ = "books"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    segment: Mapped[str] = mapped_column(String(16))
    style: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LedgerEntryRow(Base):
    """Append-only. A wallet balance is always SUM(amount) — there is no mutable balance."""

    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"))
    account: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    trade_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fill_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (Index("ix_ledger_book_account_ts", "book_id", "account", "ts"),)


class TradeRow(Base):
    """One round trip: entry -> exit."""

    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"))
    account: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16))
    instrument_id: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    contract_size: Mapped[Decimal] = mapped_column(DecimalText)
    leverage: Mapped[Decimal] = mapped_column(DecimalText)
    avg_entry: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    avg_exit: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    initial_sl: Mapped[Decimal] = mapped_column(DecimalText)
    current_sl: Mapped[Decimal] = mapped_column(DecimalText)
    target: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    trailing: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    initial_risk: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    gross_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    charges: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    funding: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    net_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    r_multiple: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    mae: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))  # worst adverse price move
    mfe: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))  # best favourable price move
    exit_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    stale_exit: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_trades_book_status", "book_id", "account", "status"),)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"))
    book_id: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16))
    instrument_id: Mapped[str] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8))
    type: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(16))
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    trigger_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    oco_group: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_orders_status", "status"),)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    trade_id: Mapped[str] = mapped_column(String(40))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(DecimalText)
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    reference_price: Mapped[Decimal] = mapped_column(DecimalText)
    reference_kind: Mapped[str] = mapped_column(String(24))
    slippage: Mapped[Decimal] = mapped_column(DecimalText)  # adverse price difference vs reference, >= 0
    liquidity: Mapped[str] = mapped_column(String(8))
    charges: Mapped[dict[str, str]] = mapped_column(JSON)
    charges_total: Mapped[Decimal] = mapped_column(DecimalText)
    stale_price: Mapped[bool] = mapped_column(Boolean, default=False)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    book_id: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16))
    instrument_id: Mapped[str] = mapped_column(String(128))
    approved: Mapped[bool] = mapped_column(Boolean)
    failed_rule_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON)

    __table_args__ = (Index("ix_risk_book_ts", "book_id", "ts"),)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    balance: Mapped[Decimal] = mapped_column(DecimalText)
    unrealized: Mapped[Decimal] = mapped_column(DecimalText)
    equity: Mapped[Decimal] = mapped_column(DecimalText)

    __table_args__ = (Index("ix_equity_book_ts", "book_id", "account", "ts"),)


class CandleRow(Base):
    __tablename__ = "candles"
    instrument_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts_open: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    open: Mapped[Decimal] = mapped_column(DecimalText)
    high: Mapped[Decimal] = mapped_column(DecimalText)
    low: Mapped[Decimal] = mapped_column(DecimalText)
    close: Mapped[Decimal] = mapped_column(DecimalText)
    volume: Mapped[Decimal] = mapped_column(DecimalText)


class AuditLogRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    category: Mapped[str] = mapped_column(String(16))
    level: Mapped[str] = mapped_column(String(8))
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_audit_cat_ts", "category", "ts"),)


class SystemStateRow(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
