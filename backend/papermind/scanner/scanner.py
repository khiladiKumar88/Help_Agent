"""Scanner: on every CLOSED signal-timeframe candle, run the book's enabled strategies.

Candle close -> FeatureFrame (closed candles only) -> regime -> strategies -> Signal
(persisted + published on Topic.SIGNAL). A strategy that raises is logged and skipped
(fail safe); it never stops the scanner. Signals before `trading_starts_at` are suppressed
(backtest warm-up). The scanner never places orders.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from papermind.core.clock import Clock
from papermind.core.config import AppConfig, BookConfig
from papermind.core.events import EventBus, Topic
from papermind.core.ids import new_id
from papermind.core.types import Candle, Signal
from papermind.data.candles import CandleBuilder, tf_seconds
from papermind.instruments.registry import InstrumentRegistry
from papermind.journal.service import Journal
from papermind.regime.detector import detect_regime
from papermind.strategies.base import Strategy, StrategyContext
from papermind.strategies.builtin import make_strategy
from papermind.strategies.features import FeatureFrame

log = logging.getLogger(__name__)

FundingFn = Callable[[str, datetime], Decimal | None]


def build_strategies(book: BookConfig) -> list[Strategy]:
    out = []
    for spec in book.strategies:
        if not spec.enabled:
            continue
        strat = make_strategy(spec.id, dict(spec.params))
        if book.market not in strat.markets:
            raise ValueError(f"strategy {spec.id} does not support market {book.market} (book {book.id})")
        out.append(strat)
    return out


class Scanner:
    def __init__(
        self,
        cfg: AppConfig,
        clock: Clock,
        bus: EventBus,
        registry: InstrumentRegistry,
        builder: CandleBuilder,
        journal: Journal,
        funding_fn: FundingFn | None = None,
        strategies: dict[str, list[Strategy]] | None = None,
        trading_starts_at: datetime | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.bus = bus
        self.registry = registry
        self.builder = builder
        self.journal = journal
        self.funding_fn = funding_fn
        self.trading_starts_at = trading_starts_at
        self.strategies = (
            strategies if strategies is not None else {b.id: build_strategies(b) for b in cfg.books.values()}
        )
        self._last_signal: dict[tuple[str, str, str], datetime] = {}
        self.errors = 0
        self.signals_emitted = 0
        bus.subscribe(Topic.CANDLE, self.on_candle)

    def _books_for(self, candle: Candle) -> list[BookConfig]:
        if candle.instrument_id not in self.registry:
            return []
        inst = self.registry.get(candle.instrument_id)
        return [
            b
            for b in self.cfg.books.values()
            if b.enabled
            and inst.symbol in b.instruments
            and inst.segment is b.segment
            and b.timeframes.get("signal") == candle.timeframe
            and self.strategies.get(b.id)
        ]

    async def on_candle(self, candle: Candle) -> None:
        if not candle.closed:
            return  # never act on a forming bar (no lookahead)
        for book in self._books_for(candle):
            try:
                await self._scan(book, candle)
            except Exception:
                self.errors += 1
                log.exception("scanner failed", extra={"book": book.id, "instrument_id": candle.instrument_id})

    async def _scan(self, book: BookConfig, candle: Candle) -> None:
        strategies = self.strategies[book.id]
        window, floats = self.builder.closed_with_floats(candle.instrument_id, candle.timeframe, book.scanner.window)
        if not window or window[-1].ts_open != candle.ts_open:
            return
        if len(window) < max(s.warmup_bars for s in strategies):
            return
        tf_s = tf_seconds(candle.timeframe)
        bar_close = candle.ts_open + timedelta(seconds=tf_s)
        if self.trading_starts_at is not None and bar_close <= self.trading_starts_at:
            return
        inst = self.registry.get(candle.instrument_id)
        tz = book.session.timezone if book.session else book.trading_day_tz
        frame = FeatureFrame(window, tz, floats)
        regime = detect_regime(frame, bar_close, book.market, tf_s)
        funding = self.funding_fn(inst.id, bar_close) if self.funding_fn and inst.is_perp else None
        ctx = StrategyContext(
            now=bar_close,
            book=book,
            instrument=inst,
            timeframe=candle.timeframe,
            frame=frame,
            regime=regime,
            funding_rate=funding,
        )
        snapshot: dict[str, object] | None = None
        for strat in strategies:
            key = (book.id, inst.id, strat.id)
            last = self._last_signal.get(key)
            if last is not None and bar_close - last < timedelta(seconds=tf_s * book.scanner.cooldown_bars):
                continue
            try:
                intent = strat.evaluate(ctx)
            except Exception:
                self.errors += 1
                log.exception("strategy failed", extra={"strategy": strat.id, "book": book.id})
                continue
            if intent is None:
                continue
            if snapshot is None:
                snapshot = {**frame.snapshot(), "funding_rate": float(funding) if funding is not None else None}
            sig = Signal(
                id=new_id("sig"),
                ts=self.clock.now(),
                book_id=book.id,
                instrument_id=inst.id,
                strategy_id=strat.id,
                params=dict(strat.params),
                timeframe=candle.timeframe,
                direction=intent.direction,
                entry_ref=intent.entry_ref,
                stop_loss=intent.stop_loss,
                target=intent.target,
                setup=intent.setup,
                regime=regime.as_dict(),
                context={**snapshot, **{f"tag_{k}": v for k, v in intent.tags.items()}},
            )
            self._last_signal[key] = bar_close
            self.signals_emitted += 1
            self.journal.save_signal(sig)
            self.journal.audit(
                "SIGNAL",
                f"{strat.id} {sig.direction} {inst.symbol} @ {sig.entry_ref} "
                f"SL {sig.stop_loss} TP {sig.target} [{regime.label}]",
                book_id=book.id,
                payload={"signal_id": sig.id},
            )
            await self.bus.publish(Topic.SIGNAL, sig)
