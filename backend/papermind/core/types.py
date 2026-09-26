"""Domain value types shared across modules (not persisted directly)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from papermind.core.config import Market, Segment
from papermind.core.money import ZERO


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is Direction.LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return self.entry_side.opposite


class Actor(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    BASELINE = "baseline"


class Account(StrEnum):
    MAIN = "main"
    SHADOW_BASELINE = "shadow_baseline"
    SHADOW_REJECTED = "shadow_rejected"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


class OrderPurpose(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    EXIT = "exit"
    SQUARE_OFF = "square_off"
    KILL = "kill"
    EXPIRY = "expiry"
    DAILY_LOSS = "daily_loss"


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TradeStatus(StrEnum):
    PENDING = "pending"  # entry order not yet filled
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"  # entry never filled


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TARGET = "target"
    MANUAL = "manual"
    SQUARE_OFF = "square_off"
    KILL_SWITCH = "kill_switch"
    EXPIRY = "expiry"
    DAILY_LOSS = "daily_loss"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class InstrumentKind(StrEnum):
    SPOT = "spot"
    PERP = "perp"
    FUT = "fut"
    CE = "ce"
    PE = "pe"


class LedgerKind(StrEnum):
    DEPOSIT = "deposit"
    REALIZED_PNL = "realized_pnl"
    CHARGE = "charge"
    FUNDING = "funding"
    ADJUSTMENT = "adjustment"


# --------------------------------------------------------------------------- market data


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Instrument(_Frozen):
    id: str
    market: Market
    segment: Segment
    exchange: str
    symbol: str
    underlying: str
    kind: InstrumentKind
    quote_ccy: str
    settle_ccy: str
    contract_size: Decimal = Decimal("1")
    lot_size: Decimal = Decimal("1")
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    expiry: datetime | None = None
    strike: Decimal | None = None
    broker_token: str | None = None
    active: bool = True

    @property
    def is_option(self) -> bool:
        return self.kind in (InstrumentKind.CE, InstrumentKind.PE)

    @property
    def is_perp(self) -> bool:
        return self.kind is InstrumentKind.PERP


class Tick(_Frozen):
    instrument_id: str
    ts: datetime
    ltp: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: Decimal | None = None
    source: str = "unknown"

    @field_validator("ltp")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError("ltp must be positive")
        return v

    @property
    def mid(self) -> Decimal:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2
        return self.ltp


class Candle(_Frozen):
    instrument_id: str
    timeframe: str
    ts_open: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO
    closed: bool = True


class FundingEvent(_Frozen):
    instrument_id: str
    ts: datetime
    rate: Decimal  # positive => longs pay shorts
    mark_price: Decimal


class TrailingConfig(_Frozen):
    """Trailing stop: once price moved `activate_after_r` R in favour, trail by distance or pct."""

    distance: Decimal | None = None
    pct: Decimal | None = None
    activate_after_r: Decimal = Decimal("1")

    @field_validator("pct")
    @classmethod
    def _pct_range(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not (ZERO < v < 100):
            raise ValueError("trailing pct must be between 0 and 100")
        return v


class OrderRequest(BaseModel):
    """A request to open a position. Always passes through RiskManager first."""

    book_id: str
    account: Account = Account.MAIN
    actor: Actor
    instrument_id: str
    direction: Direction
    qty: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    trailing: TrailingConfig | None = None
    leverage: Decimal = Decimal("1")
    signal_id: str | None = None
    note: str = ""


class ChargeBreakdown(_Frozen):
    brokerage: Decimal = ZERO
    stt: Decimal = ZERO
    exchange_txn: Decimal = ZERO
    sebi: Decimal = ZERO
    stamp: Decimal = ZERO
    ipft: Decimal = ZERO
    gst: Decimal = ZERO
    exchange_fee: Decimal = ZERO  # crypto maker/taker fee

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.stamp
            + self.ipft
            + self.gst
            + self.exchange_fee
        )

    def as_dict(self) -> dict[str, str]:
        d = {k: format(v, "f") for k, v in self.model_dump().items()}
        d["total"] = format(self.total, "f")
        return d


def trading_date(ts: datetime, tz: str) -> date:
    from zoneinfo import ZoneInfo

    return ts.astimezone(ZoneInfo(tz)).date()
