"""MarketState (latest quotes) and MarketHub (the single entry point for market data).

Providers push ticks/candles/funding into the hub; the hub updates state, aggregates
candles, persists closed candles and publishes events on the bus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from papermind.core.clock import Clock
from papermind.core.events import EventBus, Topic
from papermind.core.types import Candle, FundingEvent, Tick
from papermind.data.candles import CandleBuilder
from papermind.db.base import Database
from papermind.db.models import CandleRow

log = logging.getLogger(__name__)


@dataclass
class FundingInfo:
    rate: Decimal
    next_ts: datetime | None
    mark_price: Decimal | None
    updated_at: datetime


class MarketState:
    def __init__(self) -> None:
        self._ticks: dict[str, Tick] = {}
        self._seen: dict[str, datetime] = {}
        self.funding: dict[str, FundingInfo] = {}

    def update(self, tick: Tick, received_at: datetime) -> None:
        prev = self._ticks.get(tick.instrument_id)
        if prev is not None and tick.ts < prev.ts:
            return  # never let an older tick overwrite a newer one
        self._ticks[tick.instrument_id] = tick
        self._seen[tick.instrument_id] = received_at

    def last(self, instrument_id: str) -> Tick | None:
        return self._ticks.get(instrument_id)

    def last_seen(self, instrument_id: str) -> datetime | None:
        return self._seen.get(instrument_id)

    def age_seconds(self, instrument_id: str, now: datetime) -> float | None:
        seen = self._seen.get(instrument_id)
        return None if seen is None else (now - seen).total_seconds()

    def is_stale(self, instrument_id: str, now: datetime, threshold: float) -> bool:
        age = self.age_seconds(instrument_id, now)
        return age is None or age > threshold

    def instrument_ids(self) -> list[str]:
        return list(self._ticks)


class MarketHub:
    def __init__(
        self, clock: Clock, bus: EventBus, state: MarketState, builder: CandleBuilder, db: Database | None = None
    ) -> None:
        self.clock = clock
        self.bus = bus
        self.state = state
        self.builder = builder
        self.db = db

    async def on_tick(self, tick: Tick) -> None:
        self.state.update(tick, self.clock.now())
        closed = self.builder.on_tick(tick)
        await self.bus.publish(Topic.TICK, tick)
        for c in closed:
            await self._emit_candle(c)

    async def on_1m_candle(self, candle: Candle) -> None:
        await self.on_base_candle(candle)

    async def on_base_candle(self, candle: Candle) -> None:
        for c in self.builder.on_base_close(candle):
            await self._emit_candle(c)

    async def on_funding(self, ev: FundingEvent) -> None:
        await self.bus.publish(Topic.FUNDING, ev)

    def seed_higher(self, candles: list[Candle]) -> None:
        self.builder.seed_timeframe(candles)

    def seed_history(self, candles: list[Candle]) -> None:
        self.builder.seed(candles)
        self._persist(candles)

    async def _emit_candle(self, c: Candle) -> None:
        if c.timeframe == self.builder.base_tf:
            self._persist([c])
        await self.bus.publish(Topic.CANDLE, c)

    def _persist(self, candles: list[Candle]) -> None:
        if self.db is None or not candles:
            return
        try:
            with self.db.session() as s:
                for c in candles:
                    s.merge(
                        CandleRow(
                            instrument_id=c.instrument_id,
                            timeframe=c.timeframe,
                            ts_open=c.ts_open,
                            open=c.open,
                            high=c.high,
                            low=c.low,
                            close=c.close,
                            volume=c.volume,
                        )
                    )
        except Exception:
            log.exception("failed to persist candles")
