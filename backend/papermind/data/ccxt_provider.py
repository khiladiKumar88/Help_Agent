"""Crypto market data via ccxt — PUBLIC endpoints only (wrapped in PublicOnlyExchange).

Live quotes: websocket (ccxt.pro watch_*) with automatic fallback to REST polling.
Candles: authoritative closed 1m candles are polled over REST (they carry real volume);
ticks only drive the forming candle for the UI.
Funding: polled; a FundingEvent is emitted when the clock passes the funding timestamp.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from papermind.core.clock import Clock
from papermind.core.config import CryptoDataConfig
from papermind.core.money import D, D_or_none
from papermind.core.types import Candle, FundingEvent, Instrument, Tick
from papermind.data.market import FundingInfo, MarketHub
from papermind.data.provider import MarketDataProvider
from papermind.data.public_only import PublicOnlyExchange
from papermind.instruments.registry import InstrumentRegistry

log = logging.getLogger(__name__)

ExchangeFactory = Callable[[str], Any]


def default_exchange_factory(exchange_id: str) -> Any:
    import ccxt.pro as ccxtpro

    cls = getattr(ccxtpro, exchange_id)
    # No credentials, ever. Rate limiting on.
    return cls({"enableRateLimit": True, "options": {"defaultType": "swap"}})


def ohlcv_to_candles(instrument_id: str, tf: str, rows: list[list[Any]], now: datetime, tf_secs: int) -> list[Candle]:
    """Convert ccxt OHLCV rows to CLOSED candles; drops the still-forming last bar."""
    out: list[Candle] = []
    for r in rows:
        ts = datetime.fromtimestamp(r[0] / 1000, UTC)
        if ts + timedelta(seconds=tf_secs) > now:
            continue  # not closed yet -> would be lookahead
        out.append(
            Candle(
                instrument_id=instrument_id,
                timeframe=tf,
                ts_open=ts,
                open=D(r[1]),
                high=D(r[2]),
                low=D(r[3]),
                close=D(r[4]),
                volume=D(r[5] or 0),
                closed=True,
            )
        )
    return out


class CcxtProvider(MarketDataProvider):
    name = "ccxt"

    def __init__(
        self,
        cfg: CryptoDataConfig,
        clock: Clock,
        hub: MarketHub,
        registry: InstrumentRegistry,
        exchange_factory: ExchangeFactory = default_exchange_factory,
    ) -> None:
        self.cfg = cfg
        self.exchange = cfg.exchange
        self.clock = clock
        self.hub = hub
        self.registry = registry
        self._factory = exchange_factory
        self._ex: PublicOnlyExchange | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._instruments: dict[str, Instrument] = {}  # symbol -> instrument
        self._quotes: dict[str, dict[str, Any]] = {}
        self._last_1m: dict[str, datetime] = {}
        self._last_funding_emitted: dict[str, datetime] = {}
        self.feed_mode: str = cfg.feed
        self.errors: int = 0
        self.last_error: str | None = None
        self.connected = False

    # ------------------------------------------------------------------ lifecycle
    @property
    def ex(self) -> PublicOnlyExchange:
        if self._ex is None:
            raise RuntimeError("provider not started")
        return self._ex

    def instrument_ids(self) -> list[str]:
        return [i.id for i in self._instruments.values()]

    async def start(self) -> None:
        self._ex = PublicOnlyExchange(self._factory(self.exchange))
        markets = await self.ex.load_markets()
        insts = self.registry.load_ccxt_markets(
            self.exchange, markets, int(self.ex.precisionMode), self.cfg.symbols, self.clock.now()
        )
        self._instruments = {i.symbol: i for i in insts}
        for inst in insts:
            try:
                await self._seed_history(inst)
            except Exception as exc:
                self._record_error("seed history", exc)
        self.connected = True
        self._tasks = [
            asyncio.create_task(self._quote_loop(), name="ccxt-quotes"),
            asyncio.create_task(self._candle_loop(), name="ccxt-candles"),
            asyncio.create_task(self._funding_loop(), name="ccxt-funding"),
        ]

    async def refresh_instruments(self) -> None:
        """Daily re-sync of contract specs from the exchange instrument master."""
        try:
            markets = await self.ex.load_markets(True)
            insts = self.registry.load_ccxt_markets(
                self.exchange, markets, int(self.ex.precisionMode), self.cfg.symbols, self.clock.now()
            )
            self._instruments.update({i.symbol: i for i in insts})
        except Exception as exc:
            self._record_error("refresh instruments", exc)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self._ex is not None:
            try:
                await self._ex.close()
            except Exception:
                log.debug("exchange close failed", exc_info=True)
        self.connected = False

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "exchange": self.exchange,
            "feed_mode": self.feed_mode,
            "connected": self.connected,
            "errors": self.errors,
            "last_error": self.last_error,
            "instruments": self.instrument_ids(),
        }

    def _record_error(self, where: str, exc: Exception) -> None:
        self.errors += 1
        self.last_error = f"{where}: {type(exc).__name__}: {exc}"[:300]
        log.warning("ccxt provider error", extra={"where": where, "error": self.last_error})

    # ------------------------------------------------------------------ history
    async def fetch_candles(
        self, instrument_id: str, timeframe: str, since: datetime | None = None, limit: int = 500
    ) -> list[Candle]:
        from papermind.data.candles import tf_seconds

        inst = self.registry.get(instrument_id)
        since_ms = int(since.timestamp() * 1000) if since else None
        rows = await self.ex.fetch_ohlcv(inst.symbol, timeframe, since_ms, limit)
        return ohlcv_to_candles(instrument_id, timeframe, rows, self.clock.now(), tf_seconds(timeframe))

    async def _seed_history(self, inst: Instrument) -> None:
        candles = await self.fetch_candles(inst.id, "1m", limit=self.cfg.history_candles)
        if candles:
            self.hub.seed_history(candles)
            self._last_1m[inst.id] = candles[-1].ts_open

    # ------------------------------------------------------------------ quotes
    def _merge_quote(self, symbol: str, **fields: Any) -> Tick | None:
        inst = self._instruments.get(symbol)
        if inst is None:
            return None
        q = self._quotes.setdefault(symbol, {})
        q.update({k: v for k, v in fields.items() if v is not None})
        last = q.get("last")
        if last is None:
            bid, ask = q.get("bid"), q.get("ask")
            if bid is None or ask is None:
                return None
            last = (D(bid) + D(ask)) / 2
        return Tick(
            instrument_id=inst.id,
            ts=self.clock.now(),
            ltp=D(last),
            bid=D_or_none(q.get("bid")),
            ask=D_or_none(q.get("ask")),
            volume=None,
            source=f"ccxt:{self.exchange}:{self.feed_mode}",
        )

    async def _emit_quote(self, symbol: str, **fields: Any) -> None:
        tick = self._merge_quote(symbol, **fields)
        if tick is not None:
            await self.hub.on_tick(tick)

    async def poll_quotes_once(self) -> None:
        symbols = list(self._instruments)
        if not symbols:
            return
        has = self.ex.has or {}
        for sym in symbols:
            t = await self.ex.fetch_ticker(sym)
            bid, ask = t.get("bid"), t.get("ask")
            if (bid is None or ask is None) and not has.get("fetchBidsAsks"):
                ob = await self.ex.fetch_order_book(sym, 5)
                bid = ob["bids"][0][0] if ob.get("bids") else None
                ask = ob["asks"][0][0] if ob.get("asks") else None
            if bid is None or ask is None:
                self._quotes.setdefault(sym, {}).update({"last": t.get("last") or t.get("close")})
            else:
                await self._emit_quote(sym, last=t.get("last") or t.get("close"), bid=bid, ask=ask)
        if has.get("fetchBidsAsks"):
            ba = await self.ex.fetch_bids_asks(symbols)
            for sym, row in ba.items():
                await self._emit_quote(sym, bid=row.get("bid"), ask=row.get("ask"))

    async def _watch_ticker(self, sym: str) -> None:
        while True:
            t = await self.ex.watch_ticker(sym)
            await self._emit_quote(sym, last=t.get("last") or t.get("close"), bid=t.get("bid"), ask=t.get("ask"))

    async def _watch_bids_asks(self, symbols: list[str]) -> None:
        while True:
            ba = await self.ex.watch_bids_asks(symbols)
            for sym, row in ba.items():
                await self._emit_quote(sym, bid=row.get("bid"), ask=row.get("ask"))

    async def _quote_loop(self) -> None:
        backoff = 1.0
        ws_failures = 0
        while True:
            try:
                if self.feed_mode == "ws":
                    symbols = list(self._instruments)
                    coros = [self._watch_ticker(s) for s in symbols]
                    if (self.ex.has or {}).get("watchBidsAsks"):
                        coros.append(self._watch_bids_asks(symbols))
                    await asyncio.gather(*coros)
                else:
                    await self.poll_quotes_once()
                    backoff = 1.0
                    await self.clock.sleep(self.cfg.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"quotes/{self.feed_mode}", exc)
                if self.feed_mode == "ws":
                    ws_failures += 1
                    if ws_failures >= 3:
                        log.warning("websocket feed failing; falling back to REST polling")
                        self.feed_mode = "poll"
                await self.clock.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ------------------------------------------------------------------ candles
    async def poll_candles_once(self) -> None:
        for inst in self._instruments.values():
            last = self._last_1m.get(inst.id)
            since = last + timedelta(minutes=1) if last else None
            candles = await self.fetch_candles(inst.id, "1m", since=since, limit=100)
            for c in candles:
                if last is None or c.ts_open > last:
                    await self.hub.on_1m_candle(c)
                    last = c.ts_open
            if last is not None:
                self._last_1m[inst.id] = last

    async def _candle_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.poll_candles_once()
                backoff = 1.0
                await self.clock.sleep(self.cfg.candle_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("candles", exc)
                await self.clock.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

    # ------------------------------------------------------------------ funding
    async def poll_funding_once(self) -> None:
        await self.emit_due_funding()  # settle a due funding before its timestamp is replaced
        for inst in self._instruments.values():
            if not inst.is_perp:
                continue
            fr = await self.ex.fetch_funding_rate(inst.symbol)
            rate = D_or_none(fr.get("fundingRate"))
            if rate is None:
                continue
            nxt = fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp")
            info = FundingInfo(
                rate=rate,
                next_ts=datetime.fromtimestamp(nxt / 1000, UTC) if nxt else None,
                mark_price=D_or_none(fr.get("markPrice")),
                updated_at=self.clock.now(),
            )
            self.hub.state.funding[inst.id] = info

    async def emit_due_funding(self) -> None:
        now = self.clock.now()
        for inst_id, info in list(self.hub.state.funding.items()):
            if info.next_ts is None or now < info.next_ts:
                continue
            if self._last_funding_emitted.get(inst_id) == info.next_ts:
                continue
            mark = info.mark_price
            if mark is None:
                tick = self.hub.state.last(inst_id)
                mark = tick.ltp if tick else None
            if mark is None:
                continue
            self._last_funding_emitted[inst_id] = info.next_ts
            await self.hub.on_funding(
                FundingEvent(instrument_id=inst_id, ts=info.next_ts, rate=info.rate, mark_price=mark)
            )

    async def _funding_loop(self) -> None:
        last_poll: datetime | None = None
        while True:
            try:
                now = self.clock.now()
                if last_poll is None or (now - last_poll).total_seconds() >= self.cfg.funding_poll_seconds:
                    await self.poll_funding_once()
                    last_poll = now
                await self.emit_due_funding()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("funding", exc)
            await self.clock.sleep(5.0)
