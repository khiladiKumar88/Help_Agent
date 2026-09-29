"""Journal: the only module that writes trading records to the database.

Ledger is append-only; balances are sums of entries (cached in memory, exact Decimal).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from papermind.core.clock import Clock
from papermind.core.config import BookConfig
from papermind.core.ids import new_id
from papermind.core.logging import REDACTOR
from papermind.core.money import ZERO
from papermind.core.types import Account, LedgerKind, Signal, TradeStatus
from papermind.db.base import Database
from papermind.db.models import (
    AuditLogRow,
    BookRow,
    EquitySnapshotRow,
    FillRow,
    LedgerEntryRow,
    OrderRow,
    RiskDecisionRow,
    SignalRow,
    SystemStateRow,
    TradeRow,
)
from papermind.journal.entities import Order, Trade

log = logging.getLogger(__name__)

Key = tuple[str, str]  # (book_id, account)


class Journal:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self._balances: dict[Key, Decimal] = defaultdict(lambda: ZERO)
        self._day: dict[Key, tuple[datetime, Decimal]] = {}
        self._load_balances()

    def _load_balances(self) -> None:
        with self.db.session() as s:
            for book_id, account, amount in s.execute(
                select(LedgerEntryRow.book_id, LedgerEntryRow.account, LedgerEntryRow.amount)
            ):
                self._balances[(book_id, account)] += amount

    # ------------------------------------------------------------------ books
    def ensure_book(self, cfg: BookConfig) -> None:
        now = self.clock.now()
        with self.db.session() as s:
            if s.get(BookRow, cfg.id) is not None:
                return
            s.add(
                BookRow(
                    id=cfg.id,
                    market=str(cfg.market),
                    segment=str(cfg.segment),
                    style=str(cfg.style),
                    currency=cfg.currency,
                    mode=str(cfg.mode),
                    enabled=cfg.enabled,
                    created_at=now,
                )
            )
        self.add_ledger(cfg.id, Account.MAIN, LedgerKind.DEPOSIT, cfg.starting_capital, note="starting capital")
        self.audit("SYSTEM", f"book {cfg.id} created with {cfg.starting_capital} {cfg.currency}", book_id=cfg.id)

    def ensure_account(self, cfg: BookConfig, account: Account) -> None:
        """Shadow accounts (e.g. the random baseline) start with the same capital as the book."""
        with self.db.session() as s:
            exists = s.scalars(
                select(LedgerEntryRow.id)
                .where(LedgerEntryRow.book_id == cfg.id, LedgerEntryRow.account == str(account))
                .limit(1)
            ).first()
        if exists is None:
            self.add_ledger(cfg.id, account, LedgerKind.DEPOSIT, cfg.starting_capital, note=f"{account} capital")

    def book_state(self, book_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            row = s.get(BookRow, book_id)
            if row is None:
                raise KeyError(book_id)
            return {
                "mode": row.mode,
                "halted": row.halted,
                "halt_reason": row.halt_reason,
                "halted_until": row.halted_until,
                "enabled": row.enabled,
            }

    def set_book_halt(self, book_id: str, halted: bool, reason: str | None, until: datetime | None) -> None:
        with self.db.session() as s:
            row = s.get(BookRow, book_id)
            if row is None:
                raise KeyError(book_id)
            row.halted, row.halt_reason, row.halted_until = halted, reason, until
        self.audit(
            "RISK",
            f"book {'halted' if halted else 'resumed'}: {reason or ''}",
            book_id=book_id,
            level="WARNING" if halted else "INFO",
            payload={"until": until.isoformat() if until else None},
        )

    def set_book_mode(self, book_id: str, mode: str) -> None:
        with self.db.session() as s:
            row = s.get(BookRow, book_id)
            if row is None:
                raise KeyError(book_id)
            row.mode = mode
        self.audit("SYSTEM", f"mode set to {mode}", book_id=book_id)

    # ------------------------------------------------------------------ ledger
    def add_ledger(
        self,
        book_id: str,
        account: Account,
        kind: LedgerKind,
        amount: Decimal,
        trade_id: str | None = None,
        fill_id: str | None = None,
        note: str = "",
        ts: datetime | None = None,
    ) -> None:
        ts = ts or self.clock.now()
        with self.db.session() as s:
            s.add(
                LedgerEntryRow(
                    book_id=book_id,
                    account=str(account),
                    ts=ts,
                    kind=str(kind),
                    amount=amount,
                    trade_id=trade_id,
                    fill_id=fill_id,
                    note=note,
                )
            )
        key = (book_id, str(account))
        self._balances[key] += amount
        day = self._day.get(key)
        if day is not None and ts >= day[0] and kind is not LedgerKind.DEPOSIT:
            self._day[key] = (day[0], day[1] + amount)

    def balance(self, book_id: str, account: Account) -> Decimal:
        return self._balances[(book_id, str(account))]

    def pnl_since(self, book_id: str, account: Account, since: datetime) -> Decimal:
        """Sum of non-deposit ledger entries since `since` (realized P&L, charges, funding)."""
        key = (book_id, str(account))
        cached = self._day.get(key)
        if cached is not None and cached[0] == since:
            return cached[1]
        total = ZERO
        with self.db.session() as s:
            for (amount,) in s.execute(
                select(LedgerEntryRow.amount).where(
                    LedgerEntryRow.book_id == book_id,
                    LedgerEntryRow.account == str(account),
                    LedgerEntryRow.ts >= since,
                    LedgerEntryRow.kind != str(LedgerKind.DEPOSIT),
                )
            ):
                total += amount
        self._day[key] = (since, total)
        return total

    # ------------------------------------------------------------------ trades / orders / fills
    def save_trade(self, t: Trade) -> None:
        with self.db.session() as s:
            s.merge(TradeRow(**t.to_row()))

    def save_order(self, o: Order) -> None:
        with self.db.session() as s:
            s.merge(OrderRow(**o.to_row()))

    def save_fill(self, **fields: Any) -> str:
        fid = new_id("fil")
        with self.db.session() as s:
            s.add(FillRow(id=fid, **fields))
        return fid

    def load_active(self) -> tuple[list[Trade], list[Order]]:
        with self.db.session() as s:
            trades = [
                Trade.from_row(r)
                for r in s.scalars(
                    select(TradeRow).where(TradeRow.status.in_([str(TradeStatus.PENDING), str(TradeStatus.OPEN)]))
                )
            ]
            orders = [Order.from_row(r) for r in s.scalars(select(OrderRow).where(OrderRow.status == "pending"))]
        return trades, orders

    def get_trade(self, trade_id: str) -> Trade | None:
        with self.db.session() as s:
            row = s.get(TradeRow, trade_id)
            return Trade.from_row(row) if row else None

    def list_trades(
        self,
        book_id: str | None = None,
        account: Account | None = None,
        status: str | None = None,
        actor: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[Trade]:
        q = select(TradeRow)
        if book_id:
            q = q.where(TradeRow.book_id == book_id)
        if account:
            q = q.where(TradeRow.account == str(account))
        if status:
            q = q.where(TradeRow.status == status)
        if actor:
            q = q.where(TradeRow.actor == actor)
        if since:
            q = q.where(TradeRow.created_at >= since)
        q = q.order_by(TradeRow.created_at.desc()).limit(limit)
        with self.db.session() as s:
            return [Trade.from_row(r) for r in s.scalars(q)]

    def count_trades_since(self, book_id: str, account: Account, since: datetime) -> int:
        live = [str(TradeStatus.PENDING), str(TradeStatus.OPEN), str(TradeStatus.CLOSED)]
        with self.db.session() as s:
            rows = s.scalars(
                select(TradeRow.id).where(
                    TradeRow.book_id == book_id,
                    TradeRow.account == str(account),
                    TradeRow.created_at >= since,
                    TradeRow.status.in_(live),
                )
            ).all()
        return len(rows)

    def fills_for_trade(self, trade_id: str) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(FillRow).where(FillRow.trade_id == trade_id).order_by(FillRow.ts)).all()
            return [{c.name: getattr(r, c.name) for c in FillRow.__table__.columns} for r in rows]

    # ------------------------------------------------------------------ risk decisions
    def record_risk_decision(
        self,
        book_id: str,
        account: Account,
        actor: str,
        instrument_id: str,
        approved: bool,
        failed_rule_id: str | None,
        message: str,
        request: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> str:
        rid = new_id("rsk")
        with self.db.session() as s:
            s.add(
                RiskDecisionRow(
                    id=rid,
                    ts=self.clock.now(),
                    book_id=book_id,
                    account=str(account),
                    actor=actor,
                    instrument_id=instrument_id,
                    approved=approved,
                    failed_rule_id=failed_rule_id,
                    message=message,
                    request=request,
                    results=results,
                )
            )
        return rid

    def list_risk_decisions(self, book_id: str, limit: int = 50, rejected_only: bool = False) -> list[dict[str, Any]]:
        q = select(RiskDecisionRow).where(RiskDecisionRow.book_id == book_id)
        if rejected_only:
            q = q.where(RiskDecisionRow.approved.is_(False))
        q = q.order_by(RiskDecisionRow.ts.desc()).limit(limit)
        with self.db.session() as s:
            return [{c.name: getattr(r, c.name) for c in RiskDecisionRow.__table__.columns} for r in s.scalars(q)]

    # ------------------------------------------------------------------ signals
    def save_signal(self, sig: Signal) -> None:
        with self.db.session() as s:
            s.add(SignalRow(**sig.model_dump(mode="python")))

    def update_signal(self, signal_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            row = s.get(SignalRow, signal_id)
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)

    def list_signals(self, book_id: str, limit: int = 50) -> list[dict[str, Any]]:
        q = select(SignalRow).where(SignalRow.book_id == book_id).order_by(SignalRow.ts.desc()).limit(limit)
        with self.db.session() as s:
            return [{c.name: getattr(r, c.name) for c in SignalRow.__table__.columns} for r in s.scalars(q)]

    # ------------------------------------------------------------------ equity
    def snapshot_equity(self, book_id: str, account: Account, balance: Decimal, unrealized: Decimal) -> None:
        with self.db.session() as s:
            s.add(
                EquitySnapshotRow(
                    book_id=book_id,
                    account=str(account),
                    ts=self.clock.now(),
                    balance=balance,
                    unrealized=unrealized,
                    equity=balance + unrealized,
                )
            )

    def equity_curve(self, book_id: str, account: Account, since: datetime | None = None) -> list[dict[str, Any]]:
        q = select(EquitySnapshotRow).where(
            EquitySnapshotRow.book_id == book_id, EquitySnapshotRow.account == str(account)
        )
        if since:
            q = q.where(EquitySnapshotRow.ts >= since)
        q = q.order_by(EquitySnapshotRow.ts)
        with self.db.session() as s:
            return [
                {"ts": r.ts, "balance": r.balance, "unrealized": r.unrealized, "equity": r.equity} for r in s.scalars(q)
            ]

    # ------------------------------------------------------------------ audit / system state
    def audit(
        self,
        category: str,
        message: str,
        level: str = "INFO",
        book_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            with self.db.session() as s:
                s.add(
                    AuditLogRow(
                        ts=self.clock.now(),
                        category=category,
                        level=level,
                        book_id=book_id,
                        message=REDACTOR.redact(message),
                        payload=REDACTOR.redact_obj(payload or {}),
                    )
                )
        except Exception:
            log.exception("audit write failed")

    def list_audit(self, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = select(AuditLogRow)
        if category:
            q = q.where(AuditLogRow.category == category)
        q = q.order_by(AuditLogRow.id.desc()).limit(limit)
        with self.db.session() as s:
            return [{c.name: getattr(r, c.name) for c in AuditLogRow.__table__.columns} for r in s.scalars(q)]

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(SystemStateRow, key)
            return dict(row.value) if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        with self.db.session() as s:
            s.merge(SystemStateRow(key=key, value=value, updated_at=self.clock.now()))
