"""Risk rules — pure functions of (RiskContext, OrderRequest) -> RuleResult.

No I/O, no LLM, no mutation. Each rule has a stable id that appears in logs and the UI.
Human, agent and baseline orders all go through the same rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from papermind.core.config import BookConfig, Segment
from papermind.core.money import PCT, ZERO
from papermind.core.types import Direction, Instrument, OrderRequest, OrderType, Tick
from papermind.risk.session import in_entry_window

RoundTripFn = Callable[[Decimal, Decimal, Decimal], Decimal]  # (qty, entry, exit) -> charges


class RuleResult(BaseModel):
    rule_id: str
    passed: bool
    message: str
    metrics: dict[str, str] = {}


@dataclass(frozen=True)
class OpenPosition:
    trade_id: str
    instrument_id: str
    direction: Direction
    qty: Decimal
    status: str  # pending | open


@dataclass(frozen=True)
class RiskContext:
    now: datetime
    book: BookConfig
    instrument: Instrument | None
    tick: Tick | None
    stale: bool
    kill_switch: bool
    book_halted: bool
    halt_reason: str | None
    balance: Decimal
    equity: Decimal
    used_margin: Decimal
    day_start_equity: Decimal
    day_pnl: Decimal  # realized today + unrealized, after (estimated) charges
    trades_today: int
    positions: list[OpenPosition] = field(default_factory=list)
    round_trip_charges: RoundTripFn | None = None

    # ---- derived helpers (pure)
    def est_entry(self, req: OrderRequest) -> Decimal | None:
        if req.order_type is OrderType.LIMIT and req.limit_price is not None:
            return req.limit_price
        if self.tick is None:
            return None
        if req.direction is Direction.LONG:
            return self.tick.ask if self.tick.ask else self.tick.ltp
        return self.tick.bid if self.tick.bid else self.tick.ltp

    def est_charges(self, req: OrderRequest, qty: Decimal | None = None) -> Decimal:
        entry = self.est_entry(req)
        if self.round_trip_charges is None or entry is None:
            return ZERO
        exit_ = req.stop_loss if req.stop_loss is not None else entry
        return self.round_trip_charges(qty if qty is not None else req.qty, entry, exit_)

    def risk_amount(self, req: OrderRequest, qty: Decimal | None = None) -> Decimal | None:
        entry = self.est_entry(req)
        if entry is None or req.stop_loss is None or self.instrument is None:
            return None
        q = qty if qty is not None else req.qty
        return abs(entry - req.stop_loss) * q * self.instrument.contract_size + self.est_charges(req, q)

    def max_risk_allowed(self) -> Decimal | None:
        r = self.book.risk
        caps = []
        if r.max_risk_per_trade_abs is not None:
            caps.append(r.max_risk_per_trade_abs)
        if r.max_risk_per_trade_pct is not None:
            caps.append(self.equity * r.max_risk_per_trade_pct / PCT)
        return min(caps) if caps else None

    def daily_loss_limit(self) -> Decimal | None:
        r = self.book.risk
        caps = []
        if r.daily_loss_limit_abs is not None:
            caps.append(r.daily_loss_limit_abs)
        if r.daily_loss_limit_pct is not None:
            caps.append(self.day_start_equity * r.daily_loss_limit_pct / PCT)
        return min(caps) if caps else None


def _ok(rule: str, msg: str = "ok", **m: object) -> RuleResult:
    return RuleResult(rule_id=rule, passed=True, message=msg, metrics={k: str(v) for k, v in m.items()})


def _fail(rule: str, msg: str, **m: object) -> RuleResult:
    return RuleResult(rule_id=rule, passed=False, message=msg, metrics={k: str(v) for k, v in m.items()})


# --------------------------------------------------------------------------- rules


def r000_order_valid(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R000_ORDER_VALID"
    inst = ctx.instrument
    if req.book_id != ctx.book.id:
        return _fail(rid, f"order is for book {req.book_id}, context is {ctx.book.id}")
    if inst is None:
        return _fail(rid, f"unknown instrument {req.instrument_id}")
    if inst.symbol not in ctx.book.instruments:
        return _fail(rid, f"{inst.symbol} is not tradable in book {ctx.book.id}")
    if not inst.active:
        return _fail(rid, f"{inst.symbol} is not active")
    if inst.segment is not ctx.book.segment:
        return _fail(rid, f"{inst.symbol} is {inst.segment}, book segment is {ctx.book.segment}")
    if req.qty < inst.min_qty:
        return _fail(rid, f"qty {req.qty} below minimum {inst.min_qty}")
    if req.qty % inst.qty_step != 0:
        return _fail(rid, f"qty {req.qty} is not a multiple of step {inst.qty_step}")
    if req.direction is Direction.SHORT and inst.segment in (Segment.SPOT, Segment.OPTIONS):
        return _fail(rid, f"short selling is not allowed on {inst.segment}")
    if req.leverage < 1:
        return _fail(rid, "leverage must be >= 1")
    if inst.segment in (Segment.SPOT, Segment.OPTIONS) and req.leverage != 1:
        return _fail(rid, f"leverage must be 1 on {inst.segment}")
    if req.order_type is OrderType.LIMIT and (req.limit_price is None or req.limit_price <= 0):
        return _fail(rid, "limit order needs a positive limit_price")
    if req.order_type is OrderType.STOP_MARKET:
        return _fail(rid, "stop-market entries are not supported")
    if req.limit_price is not None and req.limit_price % inst.tick_size != 0:
        return _fail(rid, f"limit price not a multiple of tick size {inst.tick_size}")
    return _ok(rid)


def r001_kill_switch(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    if ctx.kill_switch:
        return _fail("R001_KILL_SWITCH", "global kill switch is engaged — all trading halted")
    return _ok("R001_KILL_SWITCH")


def r002_book_halted(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    if not ctx.book.enabled:
        return _fail("R002_BOOK_HALTED", f"book {ctx.book.id} is disabled")
    if ctx.book_halted:
        return _fail("R002_BOOK_HALTED", f"book halted: {ctx.halt_reason or 'no reason given'}")
    return _ok("R002_BOOK_HALTED")


def r003_session_hours(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    if ctx.book.session is None:
        return _ok("R003_SESSION_HOURS", "24x7 market")
    ok, msg = in_entry_window(ctx.book.session, ctx.now)
    return _ok("R003_SESSION_HOURS", msg) if ok else _fail("R003_SESSION_HOURS", msg)


def r004_stale_data(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    if ctx.tick is None:
        return _fail("R004_STALE_DATA", "no market data received for this instrument")
    if ctx.stale:
        return _fail("R004_STALE_DATA", "market data is stale — no new entries", last_tick=ctx.tick.ts.isoformat())
    return _ok("R004_STALE_DATA")


def r005_stop_loss_valid(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R005_STOP_LOSS_VALID"
    entry = ctx.est_entry(req)
    if req.stop_loss is None:
        return _fail(rid, "a stop-loss is required on every entry")
    if req.stop_loss <= 0:
        return _fail(rid, "stop-loss must be positive")
    if entry is None:
        return _fail(rid, "cannot validate stop-loss without a price")
    if ctx.instrument and req.stop_loss % ctx.instrument.tick_size != 0:
        return _fail(rid, f"stop-loss not a multiple of tick size {ctx.instrument.tick_size}")
    sign = req.direction.sign
    if sign * (entry - req.stop_loss) <= 0:
        side = "below" if sign > 0 else "above"
        return _fail(rid, f"stop-loss {req.stop_loss} must be {side} entry {entry} for a {req.direction}")
    if req.target is not None:
        if ctx.instrument and req.target % ctx.instrument.tick_size != 0:
            return _fail(rid, f"target not a multiple of tick size {ctx.instrument.tick_size}")
        if sign * (req.target - entry) <= 0:
            side = "above" if sign > 0 else "below"
            return _fail(rid, f"target {req.target} must be {side} entry {entry} for a {req.direction}")
    return _ok(rid, entry=entry, stop_loss=req.stop_loss)


def r006_max_risk_per_trade(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R006_MAX_RISK_PER_TRADE"
    risk = ctx.risk_amount(req)
    cap = ctx.max_risk_allowed()
    if risk is None:
        return _fail(rid, "cannot compute trade risk")
    if cap is not None and risk > cap:
        return _fail(
            rid,
            f"risk {risk:.2f} (incl. est. charges) exceeds max {cap:.2f} {ctx.book.currency}",
            risk=risk,
            max_risk=cap,
        )
    return _ok(rid, risk=risk, max_risk=cap)


def r007_max_size(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R007_MAX_SIZE"
    r = ctx.book.risk
    lots = req.qty / ctx.instrument.lot_size if ctx.instrument else req.qty
    if r.max_lots is not None and lots > r.max_lots:
        return _fail(rid, f"{lots} lots exceeds max {r.max_lots}", lots=lots)
    if r.max_qty is not None and req.qty > r.max_qty:
        return _fail(rid, f"qty {req.qty} exceeds max {r.max_qty}")
    return _ok(rid, lots=lots)


def r008_max_premium(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R008_MAX_PREMIUM"
    cap = ctx.book.risk.max_premium
    if ctx.instrument is None or not ctx.instrument.is_option or cap is None:
        return _ok(rid, "not applicable")
    entry = ctx.est_entry(req)
    if entry is None or entry > cap:
        return _fail(rid, f"premium {entry} exceeds cap {cap}")
    return _ok(rid, premium=entry)


def r009_max_open_positions(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R009_MAX_OPEN_POSITIONS"
    n, cap = len(ctx.positions), ctx.book.risk.max_open_positions
    if n >= cap:
        return _fail(rid, f"{n} open/pending positions — max is {cap}")
    return _ok(rid, open=n, max=cap)


def r010_max_trades_per_day(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R010_MAX_TRADES_PER_DAY"
    cap = ctx.book.risk.max_trades_per_day
    if cap is not None and ctx.trades_today >= cap:
        return _fail(rid, f"{ctx.trades_today} trades today — max is {cap}")
    return _ok(rid, trades_today=ctx.trades_today, max=cap)


def r011_daily_loss_limit(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R011_DAILY_LOSS_LIMIT"
    limit = ctx.daily_loss_limit()
    if limit is None:
        return _ok(rid, "no limit configured")
    if ctx.day_pnl <= -limit:
        return _fail(rid, f"daily loss {ctx.day_pnl:.2f} hit the limit {limit:.2f} — book halted for the day")
    risk = ctx.risk_amount(req) or ZERO
    if ctx.day_pnl - risk < -limit:
        return _fail(
            rid,
            f"if stopped out, day P&L {ctx.day_pnl - risk:.2f} would breach the daily loss limit {limit:.2f}",
            day_pnl=ctx.day_pnl,
            risk=risk,
        )
    return _ok(rid, day_pnl=ctx.day_pnl, limit=limit)


def r012_no_averaging_down(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R012_NO_AVERAGING_DOWN"
    for p in ctx.positions:
        if p.instrument_id == req.instrument_id:
            return _fail(
                rid,
                f"already have a {p.status} {p.direction} position on this instrument "
                "— adding to positions is not allowed",
            )
    return _ok(rid)


def r013_max_leverage(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    cap = ctx.book.risk.max_leverage
    if req.leverage > cap:
        return _fail("R013_MAX_LEVERAGE", f"leverage {req.leverage}x exceeds max {cap}x")
    return _ok("R013_MAX_LEVERAGE", leverage=req.leverage)


def r014_sufficient_margin(ctx: RiskContext, req: OrderRequest) -> RuleResult:
    rid = "R014_SUFFICIENT_MARGIN"
    entry = ctx.est_entry(req)
    if entry is None or ctx.instrument is None:
        return _fail(rid, "cannot compute margin")
    notional = entry * req.qty * ctx.instrument.contract_size
    need = notional / req.leverage + ctx.est_charges(req)
    available = ctx.equity - ctx.used_margin
    if need > available:
        return _fail(rid, f"needs {need:.2f} margin, only {available:.2f} available", need=need, available=available)
    return _ok(rid, need=need, available=available)


RULES: list[Callable[[RiskContext, OrderRequest], RuleResult]] = [
    r000_order_valid,
    r001_kill_switch,
    r002_book_halted,
    r003_session_hours,
    r004_stale_data,
    r005_stop_loss_valid,
    r013_max_leverage,
    r007_max_size,
    r008_max_premium,
    r012_no_averaging_down,
    r009_max_open_positions,
    r010_max_trades_per_day,
    r006_max_risk_per_trade,
    r011_daily_loss_limit,
    r014_sufficient_margin,
]
