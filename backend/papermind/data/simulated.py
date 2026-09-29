"""Deterministic simulated crypto feed (seeded random walk).

For offline development, demos and end-to-end tests. Everything it produces is tagged
source="simulated" / exchange "simulated", and the UI labels it as SIMULATED.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from papermind.core.clock import Clock
from papermind.core.config import CryptoDataConfig, Market, Segment
from papermind.core.money import D, round_to_step
from papermind.core.types import Candle, FundingEvent, Instrument, InstrumentKind, Tick
from papermind.data.candles import bucket_start
from papermind.data.market import FundingInfo, MarketHub
from papermind.data.provider import MarketDataProvider
from papermind.instruments.registry import InstrumentRegistry, crypto_instrument_id

SIM_EXCHANGE = "simulated"
_SECONDS_PER_YEAR = 365 * 24 * 3600
FUNDING_HOURS = (0, 8, 16)
SIM_FUNDING_RATE = Decimal("0.0001")


def _sim_instrument(symbol: str, price: Decimal) -> Instrument:
    base, rest = symbol.split("/")
    quote = rest.split(":")[0]
    return Instrument(
        id=crypto_instrument_id(SIM_EXCHANGE, symbol),
        market=Market.CRYPTO,
        segment=Segment.FUTURES,
        exchange=SIM_EXCHANGE,
        symbol=symbol,
        underlying=base,
        kind=InstrumentKind.PERP,
        quote_ccy=quote,
        settle_ccy=quote,
        contract_size=Decimal(1),
        lot_size=Decimal(1),
        tick_size=Decimal("0.1") if price >= 10000 else Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
    )


class SimulatedProvider(MarketDataProvider):
    name = "simulated"
    exchange = SIM_EXCHANGE

    def __init__(self, cfg: CryptoDataConfig, clock: Clock, hub: MarketHub, registry: InstrumentRegistry) -> None:
        self.cfg = cfg
        self.sim = cfg.simulated
        self.clock = clock
        self.hub = hub
        self.registry = registry
        self._rng = random.Random(self.sim.seed)
        self._prices: dict[str, Decimal] = {}
        self._insts: dict[str, Instrument] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_funding: datetime | None = None
        self.running = False

    def instrument_ids(self) -> list[str]:
        return list(self._insts)

    def _step(self, price: Decimal, dt_seconds: float) -> Decimal:
        sigma = float(self.sim.annual_vol) * math.sqrt(dt_seconds / _SECONDS_PER_YEAR)
        return price * D(round(math.exp(self._rng.gauss(0.0, sigma)), 10))

    async def start(self) -> None:
        now = self.clock.now()
        insts = []
        for sym in self.cfg.symbols:
            start = self.sim.start_prices.get(sym, Decimal("100"))
            inst = _sim_instrument(sym, start)
            insts.append(inst)
            self._insts[inst.id] = inst
            self._prices[inst.id] = start
        self.registry.upsert(insts, now)
        for inst in insts:
            self.hub.seed_history(self._history(inst, now))
            self.hub.state.funding[inst.id] = FundingInfo(SIM_FUNDING_RATE, self._next_funding(now), None, now)
        self.running = True
        self._task = asyncio.create_task(self._run(), name="simulated-feed")

    def _history(self, inst: Instrument, now: datetime) -> list[Candle]:
        """Backfill closed 1m candles that end at the start price (walk backwards)."""
        # enough 1m history that the aggregated signal timeframes are warm immediately (300 x 15m)
        n = max(self.cfg.history_candles, self.cfg.seed_candles * 15)
        end = bucket_start(now, "1m")
        price = self._prices[inst.id]
        closes = [price]
        for _ in range(n - 1):
            closes.append(self._step(closes[-1], 60))
        closes.reverse()
        out = []
        prev = closes[0]
        for i, close in enumerate(closes):
            ts = end - timedelta(minutes=n - i)
            hi = max(prev, close) * D("1.0004")
            lo = min(prev, close) * D("0.9996")
            out.append(
                Candle(
                    instrument_id=inst.id,
                    timeframe="1m",
                    ts_open=ts,
                    open=round_to_step(prev, inst.tick_size),
                    high=round_to_step(hi, inst.tick_size),
                    low=round_to_step(lo, inst.tick_size),
                    close=round_to_step(close, inst.tick_size),
                    volume=D(round(self._rng.uniform(5, 50), 3)),
                )
            )
            prev = close
        return out

    @staticmethod
    def _next_funding(now: datetime) -> datetime:
        day = now.replace(minute=0, second=0, microsecond=0)
        for h in FUNDING_HOURS:
            cand = day.replace(hour=h)
            if cand > now:
                return cand
        return (day + timedelta(days=1)).replace(hour=0)

    async def tick_once(self) -> None:
        now = self.clock.now()
        spread_frac = self.sim.spread_bps / Decimal(20000)
        for inst_id, inst in self._insts.items():
            p = self._step(self._prices[inst_id], self.sim.tick_interval_seconds)
            self._prices[inst_id] = p
            ltp = round_to_step(p, inst.tick_size)
            bid = round_to_step(p * (1 - spread_frac), inst.tick_size, "down")
            ask = round_to_step(p * (1 + spread_frac), inst.tick_size, "up")
            if ask <= bid:
                ask = bid + inst.tick_size
            await self.hub.on_tick(
                Tick(
                    instrument_id=inst_id,
                    ts=now,
                    ltp=ltp,
                    bid=bid,
                    ask=ask,
                    volume=D(round(self._rng.uniform(0.01, 0.5), 3)),
                    source="simulated",
                )
            )
            info = self.hub.state.funding.get(inst_id)
            if info and info.next_ts and now >= info.next_ts:
                await self.hub.on_funding(
                    FundingEvent(instrument_id=inst_id, ts=info.next_ts, rate=info.rate, mark_price=ltp)
                )
                self.hub.state.funding[inst_id] = FundingInfo(info.rate, self._next_funding(now), ltp, now)

    async def _run(self) -> None:
        while True:
            await self.tick_once()
            await self.clock.sleep(self.sim.tick_interval_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.running = False

    async def fetch_candles(
        self, instrument_id: str, timeframe: str, since: datetime | None = None, limit: int = 500
    ) -> list[Candle]:
        candles = self.hub.builder.closed(instrument_id, timeframe)
        if since:
            candles = [c for c in candles if c.ts_open >= since]
        return candles[-limit:]

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "exchange": self.exchange,
            "feed_mode": "simulated",
            "connected": self.running,
            "errors": 0,
            "last_error": None,
            "instruments": self.instrument_ids(),
        }
