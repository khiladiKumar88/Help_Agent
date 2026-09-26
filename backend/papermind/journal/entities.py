"""Mutable trade/order state used by the broker, mirrored 1:1 to DB rows by the Journal."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from papermind.core.money import ZERO
from papermind.core.types import (
    Account,
    Actor,
    Direction,
    ExitReason,
    OrderPurpose,
    OrderStatus,
    OrderType,
    Side,
    TradeStatus,
    TrailingConfig,
)


@dataclass
class Trade:
    id: str
    book_id: str
    account: Account
    actor: Actor
    instrument_id: str
    direction: Direction
    status: TradeStatus
    qty: Decimal
    contract_size: Decimal
    leverage: Decimal
    initial_sl: Decimal
    current_sl: Decimal
    created_at: datetime
    target: Decimal | None = None
    trailing: TrailingConfig | None = None
    avg_entry: Decimal | None = None
    avg_exit: Decimal | None = None
    initial_risk: Decimal | None = None
    gross_pnl: Decimal = ZERO
    charges: Decimal = ZERO
    funding: Decimal = ZERO
    net_pnl: Decimal = ZERO
    r_multiple: Decimal | None = None
    mae: Decimal = ZERO
    mfe: Decimal = ZERO
    exit_reason: ExitReason | None = None
    stale_exit: bool = False
    signal_id: str | None = None
    note: str = ""
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status in (TradeStatus.PENDING, TradeStatus.OPEN)

    def notional(self, price: Decimal) -> Decimal:
        return price * self.qty * self.contract_size

    def gross_unrealized(self, price: Decimal) -> Decimal:
        if self.avg_entry is None or self.status is not TradeStatus.OPEN:
            return ZERO
        return self.direction.sign * (price - self.avg_entry) * self.qty * self.contract_size

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["trailing"] = self.trailing.model_dump(mode="json") if self.trailing else None
        for k in ("account", "actor", "direction", "status", "exit_reason"):
            if d[k] is not None:
                d[k] = str(d[k])
        return d

    @classmethod
    def from_row(cls, row: Any) -> Trade:
        return cls(
            id=row.id,
            book_id=row.book_id,
            account=Account(row.account),
            actor=Actor(row.actor),
            instrument_id=row.instrument_id,
            direction=Direction(row.direction),
            status=TradeStatus(row.status),
            qty=row.qty,
            contract_size=row.contract_size,
            leverage=row.leverage,
            initial_sl=row.initial_sl,
            current_sl=row.current_sl,
            created_at=row.created_at,
            target=row.target,
            trailing=TrailingConfig.model_validate(row.trailing) if row.trailing else None,
            avg_entry=row.avg_entry,
            avg_exit=row.avg_exit,
            initial_risk=row.initial_risk,
            gross_pnl=row.gross_pnl,
            charges=row.charges,
            funding=row.funding,
            net_pnl=row.net_pnl,
            r_multiple=row.r_multiple,
            mae=row.mae,
            mfe=row.mfe,
            exit_reason=ExitReason(row.exit_reason) if row.exit_reason else None,
            stale_exit=row.stale_exit,
            signal_id=row.signal_id,
            note=row.note,
            opened_at=row.opened_at,
            closed_at=row.closed_at,
        )


@dataclass
class Order:
    id: str
    trade_id: str
    book_id: str
    account: Account
    actor: Actor
    instrument_id: str
    side: Side
    type: OrderType
    purpose: OrderPurpose
    qty: Decimal
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    oco_group: str | None = None
    reject_reason: str | None = None
    evaluated: bool = field(default=False, compare=False)  # saw >= 1 tick without filling (resting)

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("evaluated")
        for k in ("account", "actor", "side", "type", "purpose", "status"):
            d[k] = str(d[k])
        return d

    @classmethod
    def from_row(cls, row: Any) -> Order:
        return cls(
            id=row.id,
            trade_id=row.trade_id,
            book_id=row.book_id,
            account=Account(row.account),
            actor=Actor(row.actor),
            instrument_id=row.instrument_id,
            side=Side(row.side),
            type=OrderType(row.type),
            purpose=OrderPurpose(row.purpose),
            qty=row.qty,
            status=OrderStatus(row.status),
            created_at=row.created_at,
            updated_at=row.updated_at,
            limit_price=row.limit_price,
            trigger_price=row.trigger_price,
            oco_group=row.oco_group,
            reject_reason=row.reject_reason,
            evaluated=True,
        )
