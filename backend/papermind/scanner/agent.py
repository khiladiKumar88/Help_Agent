"""Agent pipeline: Signal -> Decider -> (mode) -> sizing -> RiskManager (inside broker.submit).

Deciders: `RuleDecider` takes every strategy signal; `analyst.LLMAnalyst` asks the LLM. Whatever
a decider says, orders still pass through the pure-code risk rules, and a decider may only make
an order STRICTER (tighter stop, smaller size) — never looser.

Mode routing (per book):
  * Manual  — signals are recorded and shown; the agent places nothing.
  * Co-pilot — a TAKE becomes a pending card the human approves or rejects. Cards expire after
    `copilot.card_ttl_bars`. A rejected card is shadow-traded in `shadow_rejected`, so we learn
    what the human's veto cost or saved.
  * Auto    — the agent places the trade itself, inside the risk rules.

Shadow accounts make the LLM measurable:
  * `shadow_raw`      — EVERY signal, unfiltered (only when an LLM filter is actually in play;
                        with the LLM off the agent already IS the raw strategy).
  * `shadow_baseline` — random entries at the same frequency.
  * `shadow_rejected` — cards the human rejected.
Each has its own wallet and its own risk limits, so they never interfere with `main`.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from papermind.broker.paper_broker import PaperBroker
from papermind.core.clock import Clock
from papermind.core.config import Mode
from papermind.core.events import EventBus, Topic
from papermind.core.money import ZERO, round_to_step
from papermind.core.types import Account, Actor, Candle, Decision, Direction, OrderRequest, Signal
from papermind.data.candles import tf_seconds
from papermind.journal.service import Journal
from papermind.risk.manager import RiskDecision

CARD_PENDING = "pending"
CARD_APPROVED = "approved"
CARD_REJECTED = "rejected"
CARD_EXPIRED = "expired"
CARD_NONE = "none"

log = logging.getLogger(__name__)


class Decider(Protocol):
    async def decide(self, signal: Signal) -> Decision: ...


class RuleDecider:
    """Takes every signal the strategies emit (the strategy IS the decision in Phase 2)."""

    async def decide(self, signal: Signal) -> Decision:
        return Decision(take=True, by="rules", reason=f"strategy {signal.strategy_id} signal (rules-only agent)")


def size_order(broker: PaperBroker, req: OrderRequest, size_factor: float | None = None) -> Decimal:
    """Largest qty allowed by the per-trade risk cap AND available margin (then the risk rules re-check).

    `size_factor` is the analyst's shrink request. It can only ever reduce the size: values of 1
    or more are ignored, so an LLM cannot talk its way into a bigger position.
    """
    book = broker.book(req.book_id)
    ctx = broker.build_context(req)
    inst = ctx.instrument
    entry = ctx.est_entry(req)
    if inst is None or entry is None or entry <= 0:
        return ZERO
    qty = broker.risk.max_qty_for_risk(ctx, req)
    if size_factor is not None and 0 < size_factor < 1:
        qty = round_to_step(qty * Decimal(str(size_factor)), inst.qty_step, "down")
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
    size_factor: float | None = None,
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
    qty = size_order(broker, base, size_factor)
    if qty <= 0:
        return None, None, "position size rounds to zero under the risk/margin limits"
    decision, trade = await broker.submit(base.model_copy(update={"qty": qty}))
    return decision, (trade.id if trade else None), decision.message


class AgentExecutor:
    """Signal -> decider -> mode routing. Also runs the shadow accounts that measure the agent."""

    def __init__(
        self,
        bus: EventBus,
        broker: PaperBroker,
        journal: Journal,
        decider: Decider | None = None,
        force_execute: bool = False,
        clock: Clock | None = None,
        shadow_raw: bool = False,
    ) -> None:
        self.bus = bus
        self.broker = broker
        self.journal = journal
        self.decider: Decider = decider or RuleDecider()
        self.force_execute = force_execute
        self.clock = clock or broker.clock
        # only worth running when something actually filters the signals (i.e. an LLM analyst)
        self.shadow_raw = shadow_raw
        bus.subscribe(Topic.SIGNAL, self.on_signal)

    # ------------------------------------------------------------------ signal
    async def on_signal(self, sig: Signal) -> None:
        # The unfiltered strategy is traded FIRST and unconditionally, so it is never contingent
        # on the decider — that is the whole point of the comparison.
        await self._shadow_raw(sig)
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
        mode = self.broker.mode(sig.book_id)
        if self.force_execute or mode == Mode.AUTO.value:
            await self._execute(sig, decision, Account.MAIN, Actor.AGENT)
            return
        if mode == Mode.COPILOT.value:
            await self._open_card(sig, decision)
        # Manual: analysis only.

    # ------------------------------------------------------------------ execution
    async def _execute(
        self, sig: Signal, decision: Decision, account: Account, actor: Actor, note: str | None = None
    ) -> str | None:
        stop = decision.stop_loss if decision.stop_loss is not None else sig.stop_loss
        rd, trade_id, _msg = await place_sized(
            self.broker,
            sig.book_id,
            account,
            actor,
            sig.instrument_id,
            sig.direction,
            stop,
            sig.target,
            sig.id,
            note or f"{sig.strategy_id}: {sig.setup}"[:500],
            size_factor=decision.size_factor,
        )
        if account is Account.MAIN:
            self.journal.update_signal(
                sig.id,
                executed=trade_id is not None,
                trade_id=trade_id,
                risk_rule_blocked=None if trade_id else (rd.failed_rule_id if rd else "SIZE_ZERO"),
            )
        return trade_id

    async def _shadow_raw(self, sig: Signal) -> None:
        """Trade every signal in `shadow_raw` — the control group for the LLM TAKE/SKIP filter."""
        if not self.shadow_raw or not self.broker.book(sig.book_id).shadow_raw_strategy:
            return
        try:
            trade_id = await self._execute(
                sig,
                Decision(take=True, by="raw", reason="unfiltered strategy signal"),
                Account.SHADOW_RAW,
                Actor.AGENT,
                note=f"raw {sig.strategy_id}",
            )
        except Exception:
            log.exception("shadow-raw entry failed")
            return
        if trade_id:
            self.journal.update_signal(sig.id, raw_trade_id=trade_id)

    # ------------------------------------------------------------------ co-pilot cards
    def _card_deadline(self, sig: Signal) -> datetime:
        cp = self.broker.book(sig.book_id).copilot
        seconds = cp.card_ttl_seconds
        if seconds is None:
            seconds = cp.card_ttl_bars * tf_seconds(sig.timeframe)
        return self.clock.now() + timedelta(seconds=seconds)

    async def _open_card(self, sig: Signal, decision: Decision) -> None:
        self.journal.update_signal(
            sig.id, card_status=CARD_PENDING, card_expires_at=self._card_deadline(sig), card_decided_at=None
        )
        self.journal.audit(
            "COPILOT",
            f"card awaiting approval: {sig.strategy_id} {sig.direction} {sig.instrument_id}",
            book_id=sig.book_id,
            payload={"signal_id": sig.id, "confidence": decision.confidence},
        )
        await self.bus.publish(Topic.AGENT_DECISION, {"signal": sig, "decision": decision, "card": CARD_PENDING})

    async def resolve_card(self, signal_id: str, approve: bool) -> dict[str, Any]:
        """Human verdict on a pending card. Approving places the trade; rejecting shadow-tracks it."""
        row = self.journal.get_signal(signal_id)
        if row is None:
            raise CardError("unknown signal")
        if row["card_status"] != CARD_PENDING:
            raise CardError(f"this card is already {row['card_status']}")
        expires = row.get("card_expires_at")
        if expires is not None and self.clock.now() > expires:
            self.journal.update_signal(signal_id, card_status=CARD_EXPIRED, card_decided_at=self.clock.now())
            raise CardError("this card has expired")
        sig = self.journal.signal_from_row(row)
        decision = Decision(
            take=approve,
            by="human",
            reason=f"human {'approved' if approve else 'rejected'} the agent card",
            confidence=row.get("confidence"),
        )
        self.journal.update_signal(
            signal_id,
            card_status=CARD_APPROVED if approve else CARD_REJECTED,
            card_decided_at=self.clock.now(),
        )
        if approve:
            trade_id = await self._execute(
                sig, decision, Account.MAIN, Actor.AGENT, note=f"approved: {sig.setup}"[:500]
            )
            return {"signal_id": signal_id, "card_status": CARD_APPROVED, "trade_id": trade_id}
        trade_id = await self._shadow_rejected(sig)
        return {"signal_id": signal_id, "card_status": CARD_REJECTED, "shadow_trade_id": trade_id}

    async def _shadow_rejected(self, sig: Signal) -> str | None:
        """What the rejected card would have done — learning data, in its own wallet."""
        if not self.broker.book(sig.book_id).copilot.shadow_track_rejected:
            return None
        try:
            trade_id = await self._execute(
                sig,
                Decision(take=True, by="human", reason="shadow of a rejected card"),
                Account.SHADOW_REJECTED,
                Actor.AGENT,
                note=f"rejected card {sig.strategy_id}",
            )
        except Exception:
            log.exception("shadow-rejected entry failed")
            return None
        if trade_id:
            self.journal.update_signal(sig.id, rejected_trade_id=trade_id)
        return trade_id

    async def expire_cards(self) -> int:
        """Called from the engine timer: mark cards whose deadline has passed."""
        now = self.clock.now()
        expired = 0
        for row in self.journal.pending_cards():
            deadline = row.get("card_expires_at")
            if deadline is not None and now > deadline:
                self.journal.update_signal(row["id"], card_status=CARD_EXPIRED, card_decided_at=now)
                self.journal.audit(
                    "COPILOT",
                    f"card expired unanswered: {row['strategy_id']} {row['direction']} {row['instrument_id']}",
                    book_id=row["book_id"],
                    payload={"signal_id": row["id"]},
                )
                expired += 1
        return expired


class CardError(ValueError):
    """A co-pilot card cannot be resolved (unknown, already decided, or expired)."""


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
