"""Agent pipeline: Signal -> Decider -> (mode) -> sizing -> RiskManager (inside broker.submit).

Phase 2 uses RuleDecider (takes every strategy signal). Phase 3 plugs the LLM analyst in as
another Decider; whatever it says, orders still pass through the pure-code risk rules.
The executor only places orders in Auto mode (or when forced, e.g. backtests/replays).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from papermind.broker.paper_broker import PaperBroker
from papermind.core.clock import Clock
from papermind.core.config import Mode
from papermind.core.events import EventBus, Topic
from papermind.core.money import ZERO, round_to_step
from papermind.core.types import Account, Actor, Candle, Decision, Direction, OrderRequest, Signal
from papermind.data.candles import tf_seconds
from papermind.journal.service import Journal
from papermind.risk.manager import RiskDecision

log = logging.getLogger(__name__)


class Decider(Protocol):
    async def decide(self, signal: Signal) -> Decision: ...


class RuleDecider:
    """Takes every signal the strategies emit (the strategy IS the decision in Phase 2)."""

    async def decide(self, signal: Signal) -> Decision:
        return Decision(take=True, by="rules", reason=f"strategy {signal.strategy_id} signal (rules-only agent)")


def size_order(broker: PaperBroker, req: OrderRequest) -> Decimal:
    """Largest qty allowed by the per-trade risk cap AND available margin (then the risk rules re-check)."""
    book = broker.book(req.book_id)
    ctx = broker.build_context(req)
    inst = ctx.instrument
    entry = ctx.est_entry(req)
    if inst is None or entry is None or entry <= 0:
        return ZERO
    qty = broker.risk.max_qty_for_risk(ctx, req)
    available = ctx.equity - ctx.used_margin
    if available <= 0:
        return ZERO
    by_margin = available * req.leverage * Decimal("0.98") / (entry * inst.contract_size)
    qty = min(qty, round_to_step(by_margin, inst.qty_step, "down"))
    if book.risk.max_qty is not None:
        qty = min(qty, book.risk.max_qty)
    if book.risk.max_lots is not None:
        qty = min(qty, round_to_step(Decimal(book.risk.max_lots) * inst.lot_size, inst.qty_step, "down"))
    return qty if qty >= inst.min_qty else ZERO


def agent_leverage(broker: PaperBroker, book_id: str) -> Decimal:
    book = broker.book(book_id)
    return book.risk.max_leverage if book.segment.value == "futures" else Decimal(1)


async def place_sized(
    broker: PaperBroker,
    book_id: str,
    account: Account,
    actor: Actor,
    instrument_id: str,
    direction: Direction,
    stop_loss: Decimal,
    target: Decimal | None,
    signal_id: str | None,
    note: str,
) -> tuple[RiskDecision | None, str | None, str]:
    """Size and submit. Returns (risk decision, trade id, message)."""
    base = OrderRequest(
        book_id=book_id,
        account=account,
        actor=actor,
        instrument_id=instrument_id,
        direction=direction,
        qty=Decimal("1"),
        stop_loss=stop_loss,
        target=target,
        leverage=agent_leverage(broker, book_id),
        signal_id=signal_id,
        note=note,
    )
    qty = size_order(broker, base)
    if qty <= 0:
        return None, None, "position size rounds to zero under the risk/margin limits"
    decision, trade = await broker.submit(base.model_copy(update={"qty": qty}))
    return decision, (trade.id if trade else None), decision.message


class AgentExecutor:
    def __init__(
        self,
        bus: EventBus,
        broker: PaperBroker,
        journal: Journal,
        decider: Decider | None = None,
        force_execute: bool = False,
    ) -> None:
        self.bus = bus
        self.broker = broker
        self.journal = journal
        self.decider: Decider = decider or RuleDecider()
        self.force_execute = force_execute
        bus.subscribe(Topic.SIGNAL, self.on_signal)

    async def on_signal(self, sig: Signal) -> None:
        try:
            decision = await self.decider.decide(sig)
        except Exception as exc:  # fail safe: decider error => NO TRADE
            log.exception("decider failed")
            decision = Decision(take=False, by="error", reason=f"decider error: {exc}")
        self.journal.update_signal(
            sig.id,
            decision="take" if decision.take else "skip",
            decision_by=decision.by,
            decision_reason=decision.reason,
        )
        await self.bus.publish(Topic.AGENT_DECISION, {"signal": sig, "decision": decision})
        if not decision.take:
            return
        if not (self.force_execute or self.broker.mode(sig.book_id) == Mode.AUTO.value):
            return  # Manual / Co-pilot: analysis only here (Co-pilot approvals arrive in Phase 3)
        rd, trade_id, _msg = await place_sized(
            self.broker,
            sig.book_id,
            Account.MAIN,
            Actor.AGENT,
            sig.instrument_id,
            sig.direction,
            sig.stop_loss,
            sig.target,
            sig.id,
            f"{sig.strategy_id}: {sig.setup}"[:500],
        )
        self.journal.update_signal(
            sig.id,
            executed=trade_id is not None,
            trade_id=trade_id,
            risk_rule_blocked=None if trade_id else (rd.failed_rule_id if rd else "SIZE_ZERO"),
        )


@dataclass
class _PendingBaseline:
    signal: Signal
    due_bars: int
    risk_distance: Decimal
    rr: Decimal | None


class BaselineAgent:
    """Random-entry benchmark in the shadow_baseline account.

    For every signal the agent decides to TAKE, the baseline opens one trade on the same instrument
    with a RANDOM direction, after a random delay of 0..max_delay_bars signal bars, with the same
    stop distance, the same reward:risk and the same sizing + risk rules. Seeded => reproducible.
    The agent has only learned something if it beats this.
    """

    def __init__(self, bus: EventBus, broker: PaperBroker, journal: Journal, clock: Clock) -> None:
        self.bus = bus
        self.broker = broker
        self.journal = journal
        self.clock = clock
        self._rng: dict[str, random.Random] = {}
        self._pending: list[_PendingBaseline] = []
        bus.subscribe(Topic.AGENT_DECISION, self.on_decision)
        bus.subscribe(Topic.CANDLE, self.on_candle)

    def _rng_for(self, book_id: str) -> random.Random:
        if book_id not in self._rng:
            self._rng[book_id] = random.Random(self.broker.book(book_id).baseline.seed)
        return self._rng[book_id]

    async def on_decision(self, payload: dict[str, object]) -> None:
        sig = payload["signal"]
        decision = payload["decision"]
        assert isinstance(sig, Signal) and isinstance(decision, Decision)
        book = self.broker.book(sig.book_id)
        if not decision.take or not book.baseline.enabled:
            return
        risk = abs(sig.entry_ref - sig.stop_loss)
        rr = abs(sig.target - sig.entry_ref) / risk if sig.target is not None and risk > 0 else None
        delay = self._rng_for(sig.book_id).randint(0, book.baseline.max_delay_bars)
        self._pending.append(_PendingBaseline(sig, delay, risk, rr))
        if delay == 0:
            await self._fire_due(sig.instrument_id, sig.timeframe)

    async def on_candle(self, candle: Candle) -> None:
        if not candle.closed or not self._pending:
            return
        half_bar = timedelta(seconds=tf_seconds(candle.timeframe) / 2)
        for p in self._pending:
            # count only bars that opened after the signal's bar closed (never the signal bar itself)
            if (
                p.signal.instrument_id == candle.instrument_id
                and p.signal.timeframe == candle.timeframe
                and candle.ts_open >= p.signal.ts - half_bar
            ):
                p.due_bars -= 1
        await self._fire_due(candle.instrument_id, candle.timeframe)

    async def _fire_due(self, instrument_id: str, tf: str) -> None:
        due = [
            p
            for p in self._pending
            if p.due_bars <= 0 and p.signal.instrument_id == instrument_id and p.signal.timeframe == tf
        ]
        self._pending = [p for p in self._pending if p not in due]
        for p in due:
            try:
                await self._enter(p)
            except Exception:
                log.exception("baseline entry failed")

    async def _enter(self, p: _PendingBaseline) -> None:
        sig = p.signal
        rng = self._rng_for(sig.book_id)
        direction = Direction.LONG if rng.random() < 0.5 else Direction.SHORT
        tick = self.broker.market.last(sig.instrument_id)
        if tick is None:
            return
        inst = self.broker.instrument(sig.instrument_id)
        px = tick.ltp
        s = direction.sign
        stop = round_to_step(px - s * p.risk_distance, inst.tick_size, "down" if s > 0 else "up")
        target = round_to_step(px + s * p.risk_distance * p.rr, inst.tick_size) if p.rr is not None else None
        _rd, trade_id, _ = await place_sized(
            self.broker,
            sig.book_id,
            Account.SHADOW_BASELINE,
            Actor.BASELINE,
            sig.instrument_id,
            direction,
            stop,
            target,
            sig.id,
            f"random baseline for {sig.strategy_id}",
        )
        if trade_id:
            self.journal.update_signal(sig.id, baseline_trade_id=trade_id)
