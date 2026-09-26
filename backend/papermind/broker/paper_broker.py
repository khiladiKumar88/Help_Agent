"""PaperBroker — simulated execution. It NEVER talks to a real broker or exchange.

Fills:   market orders fill on the next tick after submission (ask for buys, bid for sells,
         LTP +/- half the estimated spread when no quote), plus the book's slippage model.
Exits:   stop-loss (stop-market, taker) and target (resting limit, maker) form an OCO pair.
         Optional trailing stop. Intraday square-off, expiry settlement, kill switch.
Money:   every fill books its charges to the ledger; closing books realized gross P&L;
         perps accrue funding. Net P&L and R-multiple are always after charges.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from papermind.broker.charges import ChargesModel, make_charges
from papermind.broker.fills import FillQuote, exit_trigger_price, market_fill, touch_price
from papermind.core.clock import Clock
from papermind.core.config import AppConfig, BookConfig
from papermind.core.events import EventBus, Topic
from papermind.core.ids import new_id
from papermind.core.money import PCT, ZERO, money, round_to_step
from papermind.core.serde import jdict, jlist, jsonable
from papermind.core.types import (
    Account,
    ExitReason,
    FundingEvent,
    Instrument,
    LedgerKind,
    Liquidity,
    OrderPurpose,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Tick,
    TradeStatus,
)
from papermind.data.market import MarketState
from papermind.instruments.registry import InstrumentRegistry
from papermind.journal.entities import Order, Trade
from papermind.journal.service import Journal
from papermind.risk.manager import RiskDecision, RiskManager
from papermind.risk.rules import OpenPosition, RiskContext
from papermind.risk.session import day_start, next_day_start, past_square_off

log = logging.getLogger(__name__)

KILL_SWITCH_KEY = "kill_switch"
_EXIT_PURPOSE = {
    ExitReason.MANUAL: OrderPurpose.EXIT,
    ExitReason.SQUARE_OFF: OrderPurpose.SQUARE_OFF,
    ExitReason.KILL_SWITCH: OrderPurpose.KILL,
    ExitReason.EXPIRY: OrderPurpose.EXPIRY,
    ExitReason.DAILY_LOSS: OrderPurpose.DAILY_LOSS,
}
_EXIT_REASON_BY_PURPOSE = {
    OrderPurpose.STOP: ExitReason.STOP_LOSS,
    OrderPurpose.TARGET: ExitReason.TARGET,
    **{v: k for k, v in _EXIT_PURPOSE.items()},
}

SettlementFn = Callable[[Trade, Instrument], Decimal | None]


class BrokerError(ValueError):
    """User-facing broker error (bad request, rule violation on modify, unknown trade)."""

    def __init__(self, message: str, rule_id: str | None = None) -> None:
        super().__init__(message)
        self.rule_id = rule_id


class PaperBroker:
    def __init__(
        self,
        cfg: AppConfig,
        clock: Clock,
        bus: EventBus,
        journal: Journal,
        registry: InstrumentRegistry,
        market: MarketState,
        risk: RiskManager | None = None,
        settlement_price: SettlementFn | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.bus = bus
        self.journal = journal
        self.registry = registry
        self.market = market
        self.risk = risk or RiskManager()
        self.settlement_price = settlement_price
        self.charges: dict[str, ChargesModel] = {k: make_charges(v) for k, v in cfg.charges.items()}
        self.stale_after = cfg.data.stale_after_seconds
        self._trades: dict[str, Trade] = {}
        self._orders: dict[str, Order] = {}
        self._book_state: dict[str, dict[str, Any]] = {}
        self._kill = False
        self._last_push: dict[str, datetime] = {}
        self._last_snapshot: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    # ================================================================== lifecycle
    def start(self) -> None:
        for book in self.cfg.books.values():
            self.journal.ensure_book(book)
            self._book_state[book.id] = self.journal.book_state(book.id)
        trades, orders = self.journal.load_active()
        self._trades = {t.id: t for t in trades}
        self._orders = {o.id: o for o in orders}
        ks = self.journal.get_state(KILL_SWITCH_KEY) or {}
        self._kill = bool(ks.get("engaged"))
        if trades:
            self.journal.audit("SYSTEM", f"broker recovered {len(trades)} active trades, {len(orders)} orders")

    # ================================================================== lookups
    def book(self, book_id: str) -> BookConfig:
        return self.cfg.book(book_id)

    def _charges(self, book: BookConfig) -> ChargesModel:
        return self.charges[book.charges]

    def instrument(self, instrument_id: str) -> Instrument:
        return self.registry.get(instrument_id)

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill

    def mode(self, book_id: str) -> str:
        return str(self._book_state[book_id]["mode"])

    def halted(self, book_id: str) -> tuple[bool, str | None]:
        st = self._book_state[book_id]
        return bool(st["halted"]), st["halt_reason"]

    def active_trades(self, book_id: str | None = None, account: Account | None = None) -> list[Trade]:
        return [
            t
            for t in self._trades.values()
            if t.is_active and (book_id is None or t.book_id == book_id) and (account is None or t.account == account)
        ]

    def _orders_for(self, trade_id: str) -> list[Order]:
        return [o for o in self._orders.values() if o.trade_id == trade_id and o.status is OrderStatus.PENDING]

    def is_stale(self, instrument_id: str) -> bool:
        return self.market.is_stale(instrument_id, self.clock.now(), self.stale_after)

    # ================================================================== valuation
    def mark_price(self, t: Trade) -> Decimal | None:
        tick = self.market.last(t.instrument_id)
        return exit_trigger_price(tick, t.direction.exit_side) if tick else None

    def est_exit_charges(self, t: Trade, price: Decimal) -> Decimal:
        inst = self.instrument(t.instrument_id)
        return (
            self._charges(self.book(t.book_id))
            .compute(inst, t.direction.exit_side, t.qty, price, Liquidity.TAKER)
            .total
        )

    def unrealized(self, book_id: str, account: Account) -> tuple[Decimal, Decimal]:
        """(gross unrealized P&L, estimated exit charges) over open trades."""
        gross = est = ZERO
        for t in self.active_trades(book_id, account):
            if t.status is not TradeStatus.OPEN:
                continue
            px = self.mark_price(t)
            if px is None:
                continue
            gross += t.gross_unrealized(px)
            est += self.est_exit_charges(t, px)
        return gross, est

    def used_margin(self, book_id: str, account: Account) -> Decimal:
        total = ZERO
        for t in self.active_trades(book_id, account):
            if t.avg_entry is not None:
                px: Decimal | None = t.avg_entry
            else:
                entry = next((o for o in self._orders_for(t.id) if o.purpose is OrderPurpose.ENTRY), None)
                tick = self.market.last(t.instrument_id)
                px = entry.limit_price if entry and entry.limit_price else (tick.ltp if tick else None)
            if px is not None:
                total += t.notional(px) / t.leverage
        return total

    def equity(self, book_id: str, account: Account) -> Decimal:
        gross, _ = self.unrealized(book_id, account)
        return self.journal.balance(book_id, account) + gross

    def _day_start(self, book: BookConfig) -> datetime:
        return day_start(self.clock.now(), book.trading_day_tz)

    def day_pnl(self, book: BookConfig, account: Account) -> Decimal:
        """Realized today (P&L, charges, funding) + open P&L after estimated exit charges."""
        gross, est = self.unrealized(book.id, account)
        return self.journal.pnl_since(book.id, account, self._day_start(book)) + gross - est

    def day_start_equity(self, book: BookConfig, account: Account) -> Decimal:
        """Balance excluding today's realized P&L/charges/funding (deposits made today count)."""
        return self.journal.balance(book.id, account) - self.journal.pnl_since(book.id, account, self._day_start(book))

    # ================================================================== risk
    def build_context(self, req: OrderRequest) -> RiskContext:
        book = self.book(req.book_id)
        inst = self.registry.get(req.instrument_id) if req.instrument_id in self.registry else None
        charges = self._charges(book)
        halted, reason = self.halted(book.id)

        def round_trip(qty: Decimal, entry: Decimal, exit_: Decimal) -> Decimal:
            assert inst is not None
            return charges.round_trip(inst, req.direction.entry_side, qty, entry, exit_)

        return RiskContext(
            now=self.clock.now(),
            book=book,
            instrument=inst,
            tick=self.market.last(req.instrument_id),
            stale=self.is_stale(req.instrument_id),
            kill_switch=self._kill,
            book_halted=halted,
            halt_reason=reason,
            balance=self.journal.balance(book.id, req.account),
            equity=self.equity(book.id, req.account),
            used_margin=self.used_margin(book.id, req.account),
            day_start_equity=self.day_start_equity(book, req.account),
            day_pnl=self.day_pnl(book, req.account),
            trades_today=self.journal.count_trades_since(book.id, req.account, self._day_start(book)),
            positions=[
                OpenPosition(t.id, t.instrument_id, t.direction, t.qty, str(t.status))
                for t in self.active_trades(book.id, req.account)
            ],
            round_trip_charges=round_trip if inst is not None else None,
        )

    def preview(self, req: OrderRequest) -> dict[str, Any]:
        ctx = self.build_context(req)
        decision = self.risk.evaluate(ctx, req)
        entry = ctx.est_entry(req)
        notional = entry * req.qty * ctx.instrument.contract_size if entry and ctx.instrument else None
        reward = None
        if req.target is not None and entry is not None and ctx.instrument is not None:
            reward = abs(req.target - entry) * req.qty * ctx.instrument.contract_size - ctx.est_charges(req)
        risk_amt = ctx.risk_amount(req)
        return jdict(
            {
                "decision": decision,
                "est_entry": entry,
                "notional": notional,
                "margin_required": notional / req.leverage if notional is not None else None,
                "available_margin": ctx.equity - ctx.used_margin,
                "risk_amount": risk_amt,
                "reward_amount": reward,
                "reward_risk": (reward / risk_amt).quantize(Decimal("0.01")) if reward and risk_amt else None,
                "est_round_trip_charges": ctx.est_charges(req),
                "max_risk_allowed": ctx.max_risk_allowed(),
                "max_qty_by_risk": self.risk.max_qty_for_risk(ctx, req),
                "stale": ctx.stale,
            }
        )

    # ================================================================== entry
    async def submit(self, req: OrderRequest) -> tuple[RiskDecision, Trade | None]:
        async with self._lock:
            ctx = self.build_context(req)
            decision = self.risk.evaluate(ctx, req)
            self.journal.record_risk_decision(
                req.book_id,
                req.account,
                str(req.actor),
                req.instrument_id,
                decision.approved,
                decision.failed_rule_id,
                decision.message,
                jdict(req),
                jlist(decision.results),
            )
            await self.bus.publish(
                Topic.RISK_DECISION,
                jsonable(
                    {
                        "book_id": req.book_id,
                        "actor": req.actor,
                        "instrument_id": req.instrument_id,
                        **decision.model_dump(),
                    }
                ),
            )
            if not decision.approved:
                log.info("order rejected by risk", extra={"rule": decision.failed_rule_id, "msg": decision.message})
                self.journal.audit(
                    "RISK",
                    f"rejected {req.direction} {req.qty} {req.instrument_id}: "
                    f"{decision.failed_rule_id} {decision.message}",
                    level="WARNING",
                    book_id=req.book_id,
                    payload={"actor": str(req.actor)},
                )
                return decision, None
            assert ctx.instrument is not None and req.stop_loss is not None
            now = self.clock.now()
            trade = Trade(
                id=new_id("trd"),
                book_id=req.book_id,
                account=req.account,
                actor=req.actor,
                instrument_id=req.instrument_id,
                direction=req.direction,
                status=TradeStatus.PENDING,
                qty=req.qty,
                contract_size=ctx.instrument.contract_size,
                leverage=req.leverage,
                initial_sl=req.stop_loss,
                current_sl=req.stop_loss,
                target=req.target,
                trailing=req.trailing,
                created_at=now,
                signal_id=req.signal_id,
                note=req.note,
            )
            order = self._new_order(
                trade, req.direction.entry_side, req.order_type, OrderPurpose.ENTRY, limit_price=req.limit_price
            )
            self._trades[trade.id] = trade
            self.journal.save_trade(trade)
            self._save_order(order)
            self.journal.audit(
                "ORDER",
                f"{req.actor} {req.direction} {req.qty} {ctx.instrument.symbol} "
                f"({req.order_type}) SL {req.stop_loss} TP {req.target}",
                book_id=req.book_id,
                payload={"trade_id": trade.id},
            )
            await self._publish_trade(trade)
            return decision, trade

    def _new_order(
        self,
        trade: Trade,
        side: Side,
        otype: OrderType,
        purpose: OrderPurpose,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        oco: str | None = None,
    ) -> Order:
        now = self.clock.now()
        o = Order(
            id=new_id("ord"),
            trade_id=trade.id,
            book_id=trade.book_id,
            account=trade.account,
            actor=trade.actor,
            instrument_id=trade.instrument_id,
            side=side,
            type=otype,
            purpose=purpose,
            qty=trade.qty,
            status=OrderStatus.PENDING,
            created_at=now,
            updated_at=now,
            limit_price=limit_price,
            trigger_price=trigger_price,
            oco_group=oco,
        )
        self._orders[o.id] = o
        return o

    def _save_order(self, o: Order) -> None:
        self.journal.save_order(o)

    def _cancel_order(self, o: Order, reason: str) -> None:
        o.status = OrderStatus.CANCELLED
        o.reject_reason = reason
        o.updated_at = self.clock.now()
        self._save_order(o)
        self._orders.pop(o.id, None)

    async def cancel(self, trade_id: str) -> Trade:
        async with self._lock:
            t = self._trades.get(trade_id)
            if t is None or t.status is not TradeStatus.PENDING:
                raise BrokerError("only pending (unfilled) entries can be cancelled")
            await self._cancel_pending_entry(t, "cancelled by user")
            return t

    async def _cancel_pending_entry(self, t: Trade, reason: str) -> None:
        for o in self._orders_for(t.id):
            self._cancel_order(o, reason)
        t.status = TradeStatus.CANCELLED
        t.closed_at = self.clock.now()
        t.note = (t.note + f" [{reason}]").strip()
        self.journal.save_trade(t)
        self._trades.pop(t.id, None)
        await self._publish_trade(t)

    # ================================================================== exits
    async def close(self, trade_id: str, reason: ExitReason = ExitReason.MANUAL) -> Trade:
        async with self._lock:
            t = self._trades.get(trade_id)
            if t is None or not t.is_active:
                raise BrokerError(f"trade {trade_id} is not active")
            if t.status is TradeStatus.PENDING:
                await self._cancel_pending_entry(t, f"cancelled: {reason}")
                return t
            await self._request_exit(t, reason)
            return t

    async def _request_exit(self, t: Trade, reason: ExitReason) -> None:
        """Replace SL/target with a market exit that fills on the next tick."""
        pending = self._orders_for(t.id)
        if any(o.purpose in _EXIT_PURPOSE.values() for o in pending):
            return  # already exiting
        for o in pending:
            self._cancel_order(o, f"replaced by {reason} exit")
        o = self._new_order(t, t.direction.exit_side, OrderType.MARKET, _EXIT_PURPOSE[reason])
        self._save_order(o)
        self.journal.audit("ORDER", f"exit requested ({reason}) for {t.id}", book_id=t.book_id)
        await self.bus.publish(Topic.ORDER, jsonable(o))

    async def _close_now(self, t: Trade, reason: ExitReason, price: Decimal | None = None) -> None:
        """Immediate exit at the last known price (kill switch / expiry). Flags stale prices."""
        for o in self._orders_for(t.id):
            self._cancel_order(o, f"cancelled by {reason}")
        inst = self.instrument(t.instrument_id)
        tick = self.market.last(t.instrument_id)
        stale = self.is_stale(t.instrument_id)
        o = self._new_order(t, t.direction.exit_side, OrderType.MARKET, _EXIT_PURPOSE[reason])
        self._save_order(o)
        if price is not None:
            q = FillQuote(price=price, reference_price=price, reference_kind="settlement", slippage=ZERO)
            stale = False
        elif tick is not None:
            q = market_fill(inst, o.side, tick, self.book(t.book_id).slippage)
        else:
            px = t.avg_entry or t.current_sl
            q = FillQuote(price=px, reference_price=px, reference_kind="no_data_entry_price", slippage=ZERO)
            stale = True
        await self._fill(o, q, Liquidity.TAKER, self.clock.now(), stale=stale)

    async def modify(
        self, trade_id: str, stop_loss: Decimal | None = None, target: Decimal | None = None, clear_target: bool = False
    ) -> Trade:
        async with self._lock:
            t = self._trades.get(trade_id)
            if t is None or t.status is not TradeStatus.OPEN or t.avg_entry is None:
                raise BrokerError("only open trades can be modified")
            inst = self.instrument(t.instrument_id)
            px = self.mark_price(t) or t.avg_entry
            sign = t.direction.sign
            if stop_loss is not None:
                if stop_loss <= 0 or stop_loss % inst.tick_size != 0:
                    raise BrokerError("stop-loss must be positive and a multiple of tick size", "R005_STOP_LOSS_VALID")
                if sign * (px - stop_loss) <= 0:
                    raise BrokerError(
                        f"stop-loss {stop_loss} is on the wrong side of price {px}", "R005_STOP_LOSS_VALID"
                    )
                if sign * (stop_loss - t.current_sl) < 0:  # loosening: re-check per-trade risk cap
                    ctx = self.build_context(
                        OrderRequest(
                            book_id=t.book_id,
                            account=t.account,
                            actor=t.actor,
                            instrument_id=t.instrument_id,
                            direction=t.direction,
                            qty=t.qty,
                            stop_loss=stop_loss,
                        )
                    )
                    cap = ctx.max_risk_allowed()
                    new_risk = abs(t.avg_entry - stop_loss) * t.qty * t.contract_size
                    if cap is not None and new_risk > cap:
                        raise BrokerError(
                            f"moving the stop to {stop_loss} risks {new_risk:.2f} > max {cap:.2f}",
                            "R006_MAX_RISK_PER_TRADE",
                        )
                t.current_sl = stop_loss
                for o in self._orders_for(t.id):
                    if o.purpose is OrderPurpose.STOP:
                        o.trigger_price, o.updated_at = stop_loss, self.clock.now()
                        self._save_order(o)
            if clear_target or target is not None:
                if target is not None and (target % inst.tick_size != 0 or sign * (target - px) <= 0):
                    raise BrokerError(
                        f"target {target} must be beyond price {px} on the profit side", "R005_STOP_LOSS_VALID"
                    )
                t.target = target
                for o in self._orders_for(t.id):
                    if o.purpose is OrderPurpose.TARGET:
                        self._cancel_order(o, "target modified")
                if target is not None:
                    self._save_order(
                        self._new_order(
                            t, t.direction.exit_side, OrderType.LIMIT, OrderPurpose.TARGET, limit_price=target, oco=t.id
                        )
                    )
            self.journal.save_trade(t)
            self.journal.audit("ORDER", f"modified {t.id}: SL {t.current_sl} TP {t.target}", book_id=t.book_id)
            await self._publish_trade(t)
            return t

    # ================================================================== market events
    async def on_tick(self, tick: Tick) -> None:
        async with self._lock:
            orders = sorted(
                (
                    o
                    for o in self._orders.values()
                    if o.instrument_id == tick.instrument_id and o.status is OrderStatus.PENDING
                ),
                key=lambda o: (0 if o.purpose is OrderPurpose.STOP else 1, o.created_at),
            )
            touched: set[str] = set()
            for o in orders:
                if o.status is not OrderStatus.PENDING or tick.ts <= o.created_at:
                    continue  # cancelled by an OCO sibling, or not yet "next tick"
                if await self._process_order(o, tick):
                    touched.add(o.book_id)  # realized P&L changed: re-check daily loss
            for t in self.active_trades():
                if t.instrument_id == tick.instrument_id and t.status is TradeStatus.OPEN:
                    self._update_excursions_and_trail(t, tick)
                    touched.add(t.book_id)
            for book_id in touched:
                await self._check_daily_loss(self.book(book_id))
                await self._push_book(book_id, throttle=True)

    async def _process_order(self, o: Order, tick: Tick) -> bool:
        """Evaluate one pending order against a tick. Returns True if it filled."""
        t = self._trades.get(o.trade_id)
        if t is None:
            self._cancel_order(o, "orphan order")
            return False
        book = self.book(o.book_id)
        inst = self.instrument(o.instrument_id)
        buy = o.side is Side.BUY
        if o.type is OrderType.MARKET:
            await self._fill(o, market_fill(inst, o.side, tick, book.slippage), Liquidity.TAKER, tick.ts)
        elif o.type is OrderType.LIMIT and o.limit_price is not None:
            if o.purpose is OrderPurpose.ENTRY:
                touch, kind = touch_price(tick, o.side, book.slippage)
                marketable = touch <= o.limit_price if buy else touch >= o.limit_price
                if marketable and not o.evaluated:
                    q = market_fill(inst, o.side, tick, book.slippage)
                    price = min(q.price, o.limit_price) if buy else max(q.price, o.limit_price)
                    await self._fill(
                        o,
                        FillQuote(price, q.reference_price, q.reference_kind, abs(price - q.reference_price)),
                        Liquidity.TAKER,
                        tick.ts,
                    )
                elif marketable:
                    await self._fill(o, FillQuote(o.limit_price, touch, kind, ZERO), Liquidity.MAKER, tick.ts)
            else:  # resting target
                px = exit_trigger_price(tick, o.side)
                if (px <= o.limit_price) if buy else (px >= o.limit_price):
                    await self._fill(o, FillQuote(o.limit_price, px, "target_touch", ZERO), Liquidity.MAKER, tick.ts)
            o.evaluated = True
        elif o.type is OrderType.STOP_MARKET and o.trigger_price is not None:
            px = exit_trigger_price(tick, o.side)
            if (px >= o.trigger_price) if buy else (px <= o.trigger_price):
                await self._fill(o, market_fill(inst, o.side, tick, book.slippage), Liquidity.TAKER, tick.ts)
        return o.status is OrderStatus.FILLED

    async def _fill(self, o: Order, q: FillQuote, liq: Liquidity, ts: datetime, stale: bool = False) -> None:
        t = self._trades[o.trade_id]
        inst = self.instrument(o.instrument_id)
        book = self.book(o.book_id)
        charges = self._charges(book).compute(inst, o.side, o.qty, q.price, liq)
        fill_id = self.journal.save_fill(
            order_id=o.id,
            trade_id=t.id,
            ts=ts,
            side=str(o.side),
            price=q.price,
            qty=o.qty,
            reference_price=q.reference_price,
            reference_kind=q.reference_kind,
            slippage=q.slippage,
            liquidity=str(liq),
            charges=charges.as_dict(),
            charges_total=charges.total,
            stale_price=stale,
        )
        o.status, o.updated_at = OrderStatus.FILLED, ts
        self._save_order(o)
        self._orders.pop(o.id, None)
        if charges.total:
            self.journal.add_ledger(
                t.book_id, t.account, LedgerKind.CHARGE, -charges.total, t.id, fill_id, f"{o.purpose} charges", ts=ts
            )
        t.charges += charges.total

        if o.purpose is OrderPurpose.ENTRY:
            t.avg_entry, t.opened_at, t.status = q.price, ts, TradeStatus.OPEN
            t.initial_risk = abs(q.price - t.initial_sl) * t.qty * t.contract_size
            t.net_pnl = -t.charges
            self._save_order(
                self._new_order(
                    t,
                    t.direction.exit_side,
                    OrderType.STOP_MARKET,
                    OrderPurpose.STOP,
                    trigger_price=t.current_sl,
                    oco=t.id,
                )
            )
            if t.target is not None:
                self._save_order(
                    self._new_order(
                        t, t.direction.exit_side, OrderType.LIMIT, OrderPurpose.TARGET, limit_price=t.target, oco=t.id
                    )
                )
            self.journal.audit(
                "TRADE",
                f"opened {t.direction} {t.qty} {inst.symbol} @ {q.price} (fees {charges.total})",
                book_id=t.book_id,
                payload={"trade_id": t.id},
            )
        else:
            assert t.avg_entry is not None
            for sib in self._orders_for(t.id):
                self._cancel_order(sib, f"OCO: {o.purpose} filled")
            t.avg_exit, t.closed_at, t.status = q.price, ts, TradeStatus.CLOSED
            t.exit_reason = _EXIT_REASON_BY_PURPOSE[o.purpose]
            t.stale_exit = stale
            t.gross_pnl = money(t.direction.sign * (q.price - t.avg_entry) * t.qty * t.contract_size)
            self.journal.add_ledger(
                t.book_id,
                t.account,
                LedgerKind.REALIZED_PNL,
                t.gross_pnl,
                t.id,
                fill_id,
                f"closed ({t.exit_reason})",
                ts=ts,
            )
            t.net_pnl = t.gross_pnl - t.charges + t.funding
            t.r_multiple = (t.net_pnl / t.initial_risk).quantize(Decimal("0.01")) if t.initial_risk else None
            self._trades.pop(t.id, None)
            self.journal.audit(
                "TRADE",
                f"closed {t.id} {t.exit_reason} @ {q.price}: net {t.net_pnl} "
                f"{book.currency} ({t.r_multiple}R){' [STALE PRICE]' if stale else ''}",
                book_id=t.book_id,
                level="WARNING" if stale else "INFO",
                payload={"trade_id": t.id},
            )
        self.journal.save_trade(t)
        await self.bus.publish(Topic.ORDER, jsonable(o))
        await self._publish_trade(t)
        if t.status is TradeStatus.CLOSED:
            self._snapshot(book.id)

    def _update_excursions_and_trail(self, t: Trade, tick: Tick) -> None:
        assert t.avg_entry is not None
        px = exit_trigger_price(tick, t.direction.exit_side)
        sign = t.direction.sign
        move = sign * (px - t.avg_entry)
        t.mfe = max(t.mfe, move)
        t.mae = max(t.mae, -move)
        tr = t.trailing
        if tr is None:
            return
        r_dist = abs(t.avg_entry - t.initial_sl)
        if r_dist <= 0 or move / r_dist < tr.activate_after_r:
            return
        dist = tr.distance if tr.distance is not None else (px * tr.pct / PCT if tr.pct is not None else None)
        if dist is None:
            return
        inst = self.instrument(t.instrument_id)
        new_sl = round_to_step(px - sign * dist, inst.tick_size, "down" if sign > 0 else "up")
        if sign * (new_sl - t.current_sl) > 0 and sign * (px - new_sl) > 0:
            t.current_sl = new_sl
            for o in self._orders_for(t.id):
                if o.purpose is OrderPurpose.STOP:
                    o.trigger_price, o.updated_at = new_sl, self.clock.now()
                    self._save_order(o)
            self.journal.save_trade(t)

    async def on_funding(self, ev: FundingEvent) -> None:
        async with self._lock:
            inst = self.instrument(ev.instrument_id) if ev.instrument_id in self.registry else None
            if inst is None or not inst.is_perp:
                return
            for t in self.active_trades():
                if t.instrument_id != ev.instrument_id or t.status is not TradeStatus.OPEN:
                    continue
                if t.opened_at is None or t.opened_at > ev.ts:
                    continue
                payment = money(-t.direction.sign * t.qty * t.contract_size * ev.mark_price * ev.rate)
                if payment == 0:
                    continue
                t.funding += payment
                t.net_pnl = -t.charges + t.funding
                self.journal.add_ledger(
                    t.book_id,
                    t.account,
                    LedgerKind.FUNDING,
                    payment,
                    t.id,
                    note=f"funding rate {ev.rate} @ {ev.mark_price}",
                    ts=self.clock.now(),
                )
                self.journal.save_trade(t)
                await self._publish_trade(t)

    # ================================================================== timers
    async def on_timer(self) -> None:
        """Called about once a second: halts expiry, square-off, instrument expiry, equity snapshots."""
        async with self._lock:
            now = self.clock.now()
            for book in self.cfg.books.values():
                st = self._book_state[book.id]
                if st["halted"] and st["halted_until"] is not None and now >= st["halted_until"]:
                    self._set_halt(book.id, False, None, None)
                    await self._push_book(book.id)
                if book.session is not None and past_square_off(book.session, now):
                    for t in self.active_trades(book.id):
                        if t.status is TradeStatus.PENDING:
                            await self._cancel_pending_entry(t, "session square-off")
                        else:
                            await self._request_exit(t, ExitReason.SQUARE_OFF)
                last = self._last_snapshot.get(book.id)
                if last is None or (now - last).total_seconds() >= self.cfg.equity_snapshot_seconds:
                    self._snapshot(book.id)
            for t in self.active_trades():
                inst = self.instrument(t.instrument_id)
                if inst.expiry is not None and now >= inst.expiry:
                    if t.status is TradeStatus.PENDING:
                        await self._cancel_pending_entry(t, "instrument expired")
                    else:
                        settle = self.settlement_price(t, inst) if self.settlement_price else None
                        await self._close_now(t, ExitReason.EXPIRY, settle)

    def _snapshot(self, book_id: str) -> None:
        gross, _ = self.unrealized(book_id, Account.MAIN)
        self.journal.snapshot_equity(book_id, Account.MAIN, self.journal.balance(book_id, Account.MAIN), gross)
        self._last_snapshot[book_id] = self.clock.now()

    async def _check_daily_loss(self, book: BookConfig) -> None:
        halted, _ = self.halted(book.id)
        if halted:
            return
        limit = self.build_limit(book)
        if limit is None:
            return
        pnl = self.day_pnl(book, Account.MAIN)
        if pnl > -limit:
            return
        until = next_day_start(self.clock.now(), book.trading_day_tz)
        self._set_halt(book.id, True, f"daily loss limit hit ({pnl:.2f} <= -{limit:.2f})", until)
        for t in self.active_trades(book.id, Account.MAIN):
            if t.status is TradeStatus.PENDING:
                await self._cancel_pending_entry(t, "daily loss limit")
            elif book.risk.flatten_on_daily_loss:
                await self._request_exit(t, ExitReason.DAILY_LOSS)
        await self._push_book(book.id)

    def build_limit(self, book: BookConfig) -> Decimal | None:
        r = book.risk
        caps = []
        if r.daily_loss_limit_abs is not None:
            caps.append(r.daily_loss_limit_abs)
        if r.daily_loss_limit_pct is not None:
            caps.append(self.day_start_equity(book, Account.MAIN) * r.daily_loss_limit_pct / PCT)
        return min(caps) if caps else None

    def _set_halt(self, book_id: str, halted: bool, reason: str | None, until: datetime | None) -> None:
        self.journal.set_book_halt(book_id, halted, reason, until)
        self._book_state[book_id].update(halted=halted, halt_reason=reason, halted_until=until)

    def set_mode(self, book_id: str, mode: str) -> None:
        self.book(book_id)
        self.journal.set_book_mode(book_id, mode)
        self._book_state[book_id]["mode"] = mode

    # ================================================================== kill switch
    async def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        async with self._lock:
            now = self.clock.now()
            self._kill = True
            self.journal.set_state(KILL_SWITCH_KEY, {"engaged": True, "at": now.isoformat(), "reason": reason})
            self.journal.audit("SYSTEM", f"KILL SWITCH ENGAGED: {reason}", level="CRITICAL")
            closed = cancelled = 0
            for t in list(self.active_trades()):
                if t.status is TradeStatus.PENDING:
                    await self._cancel_pending_entry(t, "kill switch")
                    cancelled += 1
                else:
                    await self._close_now(t, ExitReason.KILL_SWITCH)
                    closed += 1
            for book in self.cfg.books.values():
                self._set_halt(book.id, True, f"kill switch: {reason}", None)
            result = {"engaged": True, "at": now, "reason": reason, "closed": closed, "cancelled": cancelled}
            await self.bus.publish(Topic.SYSTEM, jsonable({"kill_switch": result}))
            for book in self.cfg.books.values():
                await self._push_book(book.id)
            return jdict(result)

    async def release_kill_switch(self) -> dict[str, Any]:
        async with self._lock:
            self._kill = False
            self.journal.set_state(KILL_SWITCH_KEY, {"engaged": False, "at": self.clock.now().isoformat()})
            self.journal.audit("SYSTEM", "kill switch released", level="WARNING")
            for book in self.cfg.books.values():
                reason = self._book_state[book.id]["halt_reason"] or ""
                if reason.startswith("kill switch"):
                    self._set_halt(book.id, False, None, None)
            await self.bus.publish(Topic.SYSTEM, {"kill_switch": {"engaged": False}})
            for book in self.cfg.books.values():
                await self._push_book(book.id)
            return {"engaged": False}

    # ================================================================== views
    def trade_view(self, t: Trade) -> dict[str, Any]:
        d = jdict(t.to_row())
        inst = self.registry.get(t.instrument_id) if t.instrument_id in self.registry else None
        d["symbol"] = inst.symbol if inst else t.instrument_id
        if t.status is TradeStatus.OPEN:
            px = self.mark_price(t)
            if px is not None:
                gross = t.gross_unrealized(px)
                est = self.est_exit_charges(t, px)
                net = gross - t.charges - est + t.funding
                d.update(
                    jsonable(
                        {
                            "mark_price": px,
                            "unrealized_gross": money(gross),
                            "est_exit_charges": est,
                            "unrealized_net": money(net),
                            "unrealized_r": (net / t.initial_risk).quantize(Decimal("0.01"))
                            if t.initial_risk
                            else None,
                            "stale": self.is_stale(t.instrument_id),
                        }
                    )
                )
        return d

    def book_summary(self, book_id: str, account: Account = Account.MAIN) -> dict[str, Any]:
        book = self.book(book_id)
        st = self._book_state[book_id]
        balance = self.journal.balance(book_id, account)
        gross, est = self.unrealized(book_id, account)
        used = self.used_margin(book_id, account)
        equity = balance + gross
        return jdict(
            {
                "book_id": book_id,
                "account": account,
                "currency": book.currency,
                "mode": st["mode"],
                "market": book.market,
                "segment": book.segment,
                "style": book.style,
                "halted": st["halted"],
                "halt_reason": st["halt_reason"],
                "halted_until": st["halted_until"],
                "kill_switch": self._kill,
                "starting_capital": book.starting_capital,
                "balance": money(balance),
                "unrealized": money(gross),
                "est_exit_charges": est,
                "equity": money(equity),
                "used_margin": money(used),
                "available_margin": money(equity - used),
                "today_pnl": money(self.day_pnl(book, account)),
                "total_pnl": money(equity - book.starting_capital),
                "positions": [self.trade_view(t) for t in self.active_trades(book_id, account)],
                "instruments": [
                    i.id for i in self.registry.all() if i.symbol in book.instruments and i.segment is book.segment
                ],
            }
        )

    async def _publish_trade(self, t: Trade) -> None:
        await self.bus.publish(Topic.TRADE, self.trade_view(t))
        await self._push_book(t.book_id)

    async def _push_book(self, book_id: str, throttle: bool = False) -> None:
        now = self.clock.now()
        last = self._last_push.get(book_id)
        if throttle and last is not None and (now - last).total_seconds() < 0.5:
            return
        self._last_push[book_id] = now
        await self.bus.publish(Topic.BOOK, self.book_summary(book_id))
