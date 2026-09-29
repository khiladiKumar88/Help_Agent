"""HistoricalReplayProvider: plays stored candles through the LIVE engine code on a ReplayClock.

For each base candle it (1) advances the clock and emits four synthetic ticks — open, the two
extremes, close — through MarketHub (so the PaperBroker fills, stops and targets exactly as
live); (2) at the candle's close time emits the closed candle (-> CandleBuilder -> scanner ->
agent -> risk -> broker); (3) steps the engine timers. Market orders placed at a close fill on
the NEXT bar's open tick — no lookahead. Tick path: a bullish bar is assumed to go
open -> low -> high -> close, a bearish bar open -> high -> low -> close (conservative for stops).
There are no bid/ask quotes in OHLCV, so fills use LTP +/- half the book's estimated spread plus
slippage, and full charges — fees and slippage can't be switched off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from papermind.core.types import Candle, FundingEvent, Instrument, Tick
from papermind.data.candles import tf_seconds
from papermind.data.provider import MarketDataProvider

if TYPE_CHECKING:
    from papermind.engine import Engine

log = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]


class ReplayCancelled(Exception):
    pass


def ohlc_path(c: Candle) -> list[tuple[float, Decimal]]:
    """(fraction of bar, price); ticks sit inside the bar so orders placed at a close fill on the next open."""
    first, second = (c.low, c.high) if c.close >= c.open else (c.high, c.low)
    return [(0.0, c.open), (1 / 3, first), (2 / 3, second), (1.0, c.close)]


class HistoricalReplayProvider(MarketDataProvider):
    name = "replay"

    def __init__(
        self,
        engine: Engine,
        instruments: list[Instrument],
        candles: dict[str, list[Candle]],
        funding: dict[str, list[tuple[datetime, Decimal]]] | None = None,
        speed: float = 0.0,
        progress: ProgressFn | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.engine = engine
        self.exchange = instruments[0].exchange if instruments else "replay"
        self.instruments = instruments
        self.candles = candles
        self.funding = funding or {}
        self.speed = speed
        self.progress = progress
        self.cancelled = cancelled
        self.bars_played = 0
        self.running = False

    def instrument_ids(self) -> list[str]:
        return [i.id for i in self.instruments]

    async def start(self) -> None:
        self.engine.registry.upsert(self.instruments, self.engine.clock.now())
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def fetch_candles(
        self, instrument_id: str, timeframe: str, since: datetime | None = None, limit: int = 500
    ) -> list[Candle]:
        return self.engine.builder.closed(instrument_id, timeframe, limit)

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "exchange": self.exchange,
            "connected": self.running,
            "feed_mode": "replay",
            "bars_played": self.bars_played,
            "instruments": self.instrument_ids(),
        }

    async def run(self) -> None:
        """Play every candle (merged across instruments by time)."""
        from papermind.core.clock import ReplayClock

        clock = self.engine.clock
        if not isinstance(clock, ReplayClock):
            raise TypeError("replays require a ReplayClock")
        hub = self.engine.hub
        events: list[tuple[datetime, str, Candle]] = sorted(
            ((c.ts_open, iid, c) for iid, cs in self.candles.items() for c in cs), key=lambda e: (e[0], e[1])
        )
        fund_idx = {iid: 0 for iid in self.funding}
        total = len(events) or 1
        for n, (ts_open, iid, c) in enumerate(events):
            if self.cancelled is not None and self.cancelled():
                raise ReplayCancelled()
            secs = tf_seconds(c.timeframe)
            for frac, px in ohlc_path(c):
                off = (
                    timedelta(milliseconds=1)
                    if frac == 0
                    else (
                        timedelta(seconds=secs) - timedelta(milliseconds=1)
                        if frac == 1
                        else timedelta(seconds=secs * frac)
                    )
                )
                t = ts_open + off
                if t > clock.now():
                    clock.set(t)
                await self._funding_due(iid, t, px, fund_idx)
                await hub.on_tick(Tick(instrument_id=iid, ts=clock.now(), ltp=px, source="replay"))
            close_t = ts_open + timedelta(seconds=secs)
            if close_t > clock.now():
                clock.set(close_t)
            await hub.on_base_candle(c)
            await self.engine.run_timers_once()
            self.bars_played += 1
            if self.progress is not None and n % 200 == 0:
                self.progress(n / total)
            if self.speed > 0:
                await asyncio.sleep(secs / self.speed)
            elif n % 500 == 0:
                await asyncio.sleep(0)  # let other tasks breathe
        if self.progress is not None:
            self.progress(1.0)

    async def _funding_due(self, iid: str, t: datetime, px: Decimal, idx: dict[str, int]) -> None:
        pts = self.funding.get(iid)
        if not pts:
            return
        i = idx[iid]
        while i < len(pts) and pts[i][0] <= t:
            ts, rate = pts[i]
            await self.engine.hub.on_funding(FundingEvent(instrument_id=iid, ts=ts, rate=rate, mark_price=px))
            i += 1
        idx[iid] = i
