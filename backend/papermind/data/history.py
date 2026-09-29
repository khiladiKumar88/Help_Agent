"""Historical market-data store (local SQLite cache) + paginated, incremental downloader.

Backtests and replays read ONLY from this store — they never call an exchange. The store
lives in its own database file (default `history.db`) because it is a disposable cache:
delete it and re-download at any time.

Downloads are incremental: only ranges before the first stored bar or after the last stored
bar are fetched (plus, optionally, internal gaps). The still-forming bar is never stored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Engine,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    and_,
    func,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from papermind.core.clock import Clock
from papermind.core.money import D
from papermind.core.types import Candle
from papermind.data.candles import tf_seconds
from papermind.data.public_only import PublicOnlyExchange
from papermind.db.base import make_engine

log = logging.getLogger(__name__)

_meta = MetaData()
ohlcv = Table(
    "ohlcv",
    _meta,
    Column("exchange", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("ts", Integer, nullable=False),  # bar open, epoch ms UTC
    Column("open", String(40), nullable=False),  # exact decimal strings
    Column("high", String(40), nullable=False),
    Column("low", String(40), nullable=False),
    Column("close", String(40), nullable=False),
    Column("volume", String(40), nullable=False),
    PrimaryKeyConstraint("exchange", "symbol", "timeframe", "ts"),
    sqlite_with_rowid=False,
)
markets = Table(
    "markets",
    _meta,
    Column("exchange", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("spec", JSON, nullable=False),  # ccxt unified market (contract specs for offline replays)
    PrimaryKeyConstraint("exchange", "symbol"),
)
funding = Table(
    "funding",
    _meta,
    Column("exchange", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("ts", Integer, nullable=False),
    Column("rate", String(40), nullable=False),
    PrimaryKeyConstraint("exchange", "symbol", "ts"),
    sqlite_with_rowid=False,
)


def to_ms(ts: datetime) -> int:
    return int(ts.astimezone(UTC).timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _dec_str(v: Any) -> str:
    return format(D(v), "f")


@dataclass(frozen=True)
class Gap:
    start: datetime  # first missing bar
    end: datetime  # last missing bar
    missing_bars: int


@dataclass
class Coverage:
    exchange: str
    symbol: str
    timeframe: str
    first: datetime | None
    last: datetime | None
    bars: int
    expected_bars: int
    missing_bars: int
    gaps: list[Gap] = field(default_factory=list)

    @property
    def complete_pct(self) -> float:
        return 100.0 if self.expected_bars == 0 else round(100.0 * self.bars / self.expected_bars, 2)


class HistoryStore:
    def __init__(self, url: str) -> None:
        self.engine: Engine = make_engine(url)
        _meta.create_all(self.engine)

    # ------------------------------------------------------------------ writes
    def upsert_candles(self, exchange: str, symbol: str, tf: str, rows: list[list[Any]]) -> int:
        if not rows:
            return 0
        values = [
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": tf,
                "ts": int(r[0]),
                "open": _dec_str(r[1]),
                "high": _dec_str(r[2]),
                "low": _dec_str(r[3]),
                "close": _dec_str(r[4]),
                "volume": _dec_str(r[5] or 0),
            }
            for r in rows
        ]
        stmt = sqlite_insert(ohlcv)
        stmt = stmt.on_conflict_do_update(
            index_elements=["exchange", "symbol", "timeframe", "ts"],
            set_={c: stmt.excluded[c] for c in ("open", "high", "low", "close", "volume")},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt, values)
        return len(values)

    def upsert_funding(self, exchange: str, symbol: str, rows: list[tuple[int, Any]]) -> int:
        if not rows:
            return 0
        values = [{"exchange": exchange, "symbol": symbol, "ts": int(ts), "rate": _dec_str(r)} for ts, r in rows]
        stmt = sqlite_insert(funding)
        stmt = stmt.on_conflict_do_update(
            index_elements=["exchange", "symbol", "ts"], set_={"rate": stmt.excluded.rate}
        )
        with self.engine.begin() as conn:
            conn.execute(stmt, values)
        return len(values)

    def save_market(self, exchange: str, symbol: str, spec: dict[str, Any]) -> None:
        stmt = sqlite_insert(markets).values(exchange=exchange, symbol=symbol, spec=spec)
        stmt = stmt.on_conflict_do_update(index_elements=["exchange", "symbol"], set_={"spec": stmt.excluded.spec})
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def market(self, exchange: str, symbol: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(markets.c.spec).where(markets.c.exchange == exchange, markets.c.symbol == symbol)
            ).first()
        return dict(row[0]) if row else None

    # ------------------------------------------------------------------ reads
    def _where(self, exchange: str, symbol: str, tf: str) -> Any:
        return and_(ohlcv.c.exchange == exchange, ohlcv.c.symbol == symbol, ohlcv.c.timeframe == tf)

    def bounds(self, exchange: str, symbol: str, tf: str) -> tuple[int, int, int] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(func.min(ohlcv.c.ts), func.max(ohlcv.c.ts), func.count()).where(
                    self._where(exchange, symbol, tf)
                )
            ).one()
        return None if row[2] == 0 else (int(row[0]), int(row[1]), int(row[2]))

    def funding_bounds(self, exchange: str, symbol: str) -> tuple[int, int] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(func.min(funding.c.ts), func.max(funding.c.ts)).where(
                    funding.c.exchange == exchange, funding.c.symbol == symbol
                )
            ).one()
        return None if row[0] is None else (int(row[0]), int(row[1]))

    def coverage(self, exchange: str, symbol: str, tf: str, max_gaps: int = 50) -> Coverage:
        b = self.bounds(exchange, symbol, tf)
        if b is None:
            return Coverage(exchange, symbol, tf, None, None, 0, 0, 0)
        first, last, count = b
        step = tf_seconds(tf) * 1000
        expected = (last - first) // step + 1
        sql = text(
            "SELECT prev, ts FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev FROM ohlcv "
            "WHERE exchange=:e AND symbol=:s AND timeframe=:tf) WHERE ts - prev > :step ORDER BY ts LIMIT :n"
        )
        gaps = []
        with self.engine.connect() as conn:
            for prev, ts in conn.execute(sql, {"e": exchange, "s": symbol, "tf": tf, "step": step, "n": max_gaps}):
                gaps.append(Gap(from_ms(prev + step), from_ms(ts - step), (ts - prev) // step - 1))
        return Coverage(exchange, symbol, tf, from_ms(first), from_ms(last), count, expected, expected - count, gaps)

    def datasets(self) -> list[dict[str, Any]]:
        q = select(
            ohlcv.c.exchange,
            ohlcv.c.symbol,
            ohlcv.c.timeframe,
            func.min(ohlcv.c.ts),
            func.max(ohlcv.c.ts),
            func.count(),
        ).group_by(ohlcv.c.exchange, ohlcv.c.symbol, ohlcv.c.timeframe)
        with self.engine.connect() as conn:
            return [
                {"exchange": e, "symbol": s, "timeframe": tf, "first": from_ms(a), "last": from_ms(z), "bars": n}
                for e, s, tf, a, z, n in conn.execute(q)
            ]

    def load_candles(
        self, exchange: str, symbol: str, tf: str, start: datetime, end: datetime, instrument_id: str
    ) -> list[Candle]:
        """Closed candles with ts_open in [start, end), oldest first."""
        q = (
            select(ohlcv.c.ts, ohlcv.c.open, ohlcv.c.high, ohlcv.c.low, ohlcv.c.close, ohlcv.c.volume)
            .where(self._where(exchange, symbol, tf), ohlcv.c.ts >= to_ms(start), ohlcv.c.ts < to_ms(end))
            .order_by(ohlcv.c.ts)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return [
            Candle.model_construct(
                instrument_id=instrument_id,
                timeframe=tf,
                ts_open=from_ms(ts),
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(lo),
                close=Decimal(c),
                volume=Decimal(v),
                closed=True,
            )
            for ts, o, h, lo, c, v in rows
        ]

    def load_funding(
        self, exchange: str, symbol: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, Decimal]]:
        q = (
            select(funding.c.ts, funding.c.rate)
            .where(
                funding.c.exchange == exchange,
                funding.c.symbol == symbol,
                funding.c.ts >= to_ms(start),
                funding.c.ts < to_ms(end),
            )
            .order_by(funding.c.ts)
        )
        with self.engine.connect() as conn:
            return [(from_ms(ts), Decimal(r)) for ts, r in conn.execute(q)]


# ============================================================================ downloader


@dataclass
class SyncReport:
    exchange: str
    symbol: str
    timeframe: str
    requests: int = 0
    bars_written: int = 0
    funding_written: int = 0
    coverage: Coverage | None = None


ExchangeFactory = Callable[[str], Any]
ProgressFn = Callable[[float, str], None]


class HistoryDownloader:
    """Paginated public OHLCV (+ funding) download into the HistoryStore."""

    def __init__(
        self,
        store: HistoryStore,
        exchange_factory: ExchangeFactory,
        clock: Clock,
        page_limit: int = 1000,
        max_retries: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.factory = exchange_factory
        self.clock = clock
        self.page_limit = page_limit
        self.max_retries = max_retries
        self.sleep = sleep

    async def _call(self, fn: Callable[[], Awaitable[Any]], report: SyncReport) -> Any:
        import ccxt

        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                report.requests += 1
                return await fn()
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("history fetch failed, retrying", extra={"error": str(exc)[:200], "attempt": attempt})
                await self.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise RuntimeError("unreachable")

    async def sync(
        self,
        exchange_id: str,
        symbol: str,
        tf: str,
        start: datetime,
        end: datetime | None = None,
        repair_gaps: bool = False,
        progress: ProgressFn | None = None,
    ) -> SyncReport:
        report = SyncReport(exchange_id, symbol, tf)
        raw = self.factory(exchange_id)
        ex = PublicOnlyExchange(raw)
        try:
            markets = await self._call(lambda: ex.load_markets(), report)
            if symbol not in markets:
                raise ValueError(f"{symbol} is not listed on {exchange_id}")
            spec = {**_json_safe(markets[symbol]), "_precision_mode": int(ex.precisionMode)}
            self.store.save_market(exchange_id, symbol, spec)
            step = tf_seconds(tf) * 1000
            end_ms = min(to_ms(end or self.clock.now()), to_ms(self.clock.now()))
            start_ms = to_ms(start) - to_ms(start) % step
            ranges: list[tuple[int, int]] = []
            b = self.store.bounds(exchange_id, symbol, tf)
            if b is None:
                ranges.append((start_ms, end_ms))
            else:
                first, last, _ = b
                if start_ms < first:
                    ranges.append((start_ms, first))
                if last + step < end_ms:
                    ranges.append((last + step, end_ms))
                if repair_gaps:
                    for g in self.store.coverage(exchange_id, symbol, tf, max_gaps=1000).gaps:
                        ranges.append((to_ms(g.start), to_ms(g.end) + step))
            total = sum(max(0, z - a) for a, z in ranges) or 1
            done = 0
            for a, z in ranges:
                async for n_ms in self._fetch_range(ex, exchange_id, symbol, tf, a, z, report):
                    done += n_ms
                    if progress:
                        progress(min(0.95, done / total), f"{symbol} {tf}: {report.bars_written} bars")
            if markets[symbol].get("swap") and (ex.has or {}).get("fetchFundingRateHistory"):
                await self._sync_funding(ex, exchange_id, symbol, start_ms, end_ms, report)
            report.coverage = self.store.coverage(exchange_id, symbol, tf)
            if progress:
                progress(1.0, "done")
            return report
        finally:
            try:
                await ex.close()
            except Exception:
                log.debug("close failed", exc_info=True)

    async def _fetch_range(
        self, ex: PublicOnlyExchange, exchange_id: str, symbol: str, tf: str, a: int, z: int, report: SyncReport
    ) -> Any:
        step = tf_seconds(tf) * 1000
        now_ms = to_ms(self.clock.now())
        since = a
        while since < z:
            rows = await self._call(lambda s=since: ex.fetch_ohlcv(symbol, tf, s, self.page_limit), report)  # type: ignore[misc]
            closed = [r for r in rows if a <= r[0] < z and r[0] + step <= now_ms]
            if not closed:
                break  # nothing more available (range end, not listed yet, or only a forming bar)
            report.bars_written += self.store.upsert_candles(exchange_id, symbol, tf, closed)
            nxt = int(closed[-1][0]) + step
            if nxt <= since:
                break  # no progress: stop instead of looping
            yield nxt - since
            since = nxt

    async def _sync_funding(
        self, ex: PublicOnlyExchange, exchange_id: str, symbol: str, start_ms: int, end_ms: int, report: SyncReport
    ) -> None:
        fb = self.store.funding_bounds(exchange_id, symbol)
        since = start_ms if fb is None or start_ms < fb[0] else fb[1] + 1
        while since < end_ms:
            rows = await self._call(lambda s=since: ex.fetch_funding_rate_history(symbol, s, self.page_limit), report)  # type: ignore[misc]
            pts = [
                (int(r["timestamp"]), r["fundingRate"])
                for r in rows
                if r.get("timestamp") is not None
                and r.get("fundingRate") is not None
                and since <= r["timestamp"] < end_ms
            ]
            if not pts:
                break
            report.funding_written += self.store.upsert_funding(exchange_id, symbol, pts)
            nxt = max(p[0] for p in pts) + 1
            if nxt <= since:
                break
            since = nxt


# ============================================================================ simulated history


class SimulatedHistoryExchange:
    """Deterministic synthetic history (offline demos/tests). Pagination-safe: every bar is a pure
    function of (seed, symbol, time). Each UTC day is a Brownian bridge between daily opens drawn
    from a regime-switching daily walk, so there are trends, ranges and volatile stretches."""

    id = "simulated"
    precisionMode = 4
    apiKey = ""
    secret = ""
    has = {"fetchOHLCV": True, "fetchFundingRateHistory": True}
    ANCHOR = datetime(2020, 1, 1, tzinfo=UTC)

    def __init__(
        self, clock: Clock, seed: int = 42, start_prices: dict[str, Decimal] | None = None, annual_vol: float = 0.6
    ) -> None:
        self.clock = clock
        self.seed = seed
        self.start_prices = start_prices or {"BTC/USDT:USDT": Decimal("60000"), "ETH/USDT:USDT": Decimal("3000")}
        self.vol = annual_vol
        self._daily: dict[str, list[float]] = {}
        self._paths: dict[tuple[str, int], Any] = {}

    def _sym_key(self, symbol: str) -> int:
        return sum(ord(c) * (i + 1) for i, c in enumerate(symbol))

    async def load_markets(self, reload: bool = False) -> dict[str, dict[str, Any]]:
        out = {}
        for sym in self.start_prices:
            base, rest = sym.split("/")
            quote = rest.split(":")[0]
            out[sym] = {
                "id": sym,
                "symbol": sym,
                "base": base,
                "quote": quote,
                "settle": quote,
                "type": "swap",
                "swap": True,
                "spot": False,
                "future": False,
                "option": False,
                "contract": True,
                "linear": True,
                "inverse": False,
                "contractSize": 1,
                "active": True,
                "precision": {"amount": 0.001, "price": 0.1 if self.start_prices[sym] >= 10000 else 0.01},
                "limits": {"amount": {"min": 0.001}},
            }
        return out

    MAX_DAYS = 366 * 16  # series is generated once, with a fixed length, so it never changes

    def _daily_opens(self, symbol: str, day: int) -> list[float]:
        import numpy as np

        if day + 1 >= self.MAX_DAYS:
            raise ValueError("simulated history only covers 2020-2035")
        if symbol in self._daily:
            return self._daily[symbol]
        n = self.MAX_DAYS
        k = self._sym_key(symbol)
        daily_sigma = self.vol / np.sqrt(365)
        drift = np.random.default_rng([self.seed, k, 1]).normal(0, daily_sigma * 0.15, size=n // 20 + 1)
        vmult = np.random.default_rng([self.seed, k, 2]).choice([0.6, 1.0, 1.8], size=n // 15 + 1, p=[0.35, 0.45, 0.2])
        shocks = np.random.default_rng([self.seed, k, 3]).normal(0, daily_sigma, size=n)
        rets = np.repeat(drift, 20)[:n] + shocks * np.repeat(vmult, 15)[:n]
        # mean-reverting in log space (half-life ~6 months) so prices stay realistic over 15 years
        anchor = float(np.log(float(self.start_prices[symbol])))
        kappa = 0.01
        logp = anchor
        prices = [float(self.start_prices[symbol])]
        for r in rets[:-1]:
            logp += kappa * (anchor - logp) + float(r)
            prices.append(float(np.exp(logp)))
        self._daily[symbol] = prices
        return self._daily[symbol]

    def _minute_path(self, symbol: str, day: int) -> Any:
        import numpy as np

        key = (symbol, day)
        if key in self._paths:
            return self._paths[key]
        opens = self._daily_opens(symbol, day + 1)
        a, b = np.log(opens[day]), np.log(opens[day + 1])
        rng = np.random.default_rng([self.seed, self._sym_key(symbol), day])
        steps = rng.normal(0, self.vol / np.sqrt(365 * 1440), size=1440)
        walk = np.concatenate([[0.0], np.cumsum(steps)])
        t = np.linspace(0, 1, 1441)
        bridge = a + walk - t * (walk[-1] - (b - a))
        path = np.exp(bridge)
        wick = np.abs(rng.normal(0, self.vol / np.sqrt(365 * 1440) * 0.8, size=1440))
        vol = rng.gamma(2.0, 10.0, size=1440)
        if len(self._paths) > 64:
            self._paths.clear()
        self._paths[key] = (path, wick, vol)
        return self._paths[key]

    def _bar(self, symbol: str, ts_ms: int, tf_ms: int, tick: float) -> list[Any]:
        minutes = tf_ms // 60000
        start_min = (ts_ms - to_ms(self.ANCHOR)) // 60000
        o = h = lo = c = 0.0
        v = 0.0
        for k in range(minutes):
            m = start_min + k
            day, i = divmod(m, 1440)
            path, wick, vol = self._minute_path(symbol, int(day))
            po, pc = float(path[i]), float(path[i + 1])
            ph = max(po, pc) * (1 + float(wick[i]))
            pl = min(po, pc) * (1 - float(wick[i]))
            if k == 0:
                o, h, lo = po, ph, pl
            h, lo, c = max(h, ph), min(lo, pl), pc
            v += float(vol[i])

        def r(x: float) -> str:
            return format((Decimal(repr(x)) / Decimal(repr(tick))).quantize(Decimal(1)) * Decimal(repr(tick)), "f")

        return [ts_ms, r(o), r(h), r(lo), r(c), f"{v:.3f}"]

    async def fetch_ohlcv(self, symbol: str, tf: str, since: int | None, limit: int) -> list[list[Any]]:
        step = tf_seconds(tf) * 1000
        now = to_ms(self.clock.now())
        start = max(since if since is not None else now - limit * step, to_ms(self.ANCHOR))
        start -= start % step
        tick = 0.1 if self.start_prices[symbol] >= 10000 else 0.01
        out: list[list[Any]] = []
        ts = start
        while len(out) < limit and ts + step <= now:
            out.append(self._bar(symbol, ts, step, tick))
            ts += step
        return out

    async def fetch_funding_rate_history(self, symbol: str, since: int | None, limit: int) -> list[dict[str, Any]]:
        import math

        step = 8 * 3600 * 1000
        now = to_ms(self.clock.now())
        ts = max(since or now - limit * step, to_ms(self.ANCHOR))
        ts += (-ts) % step
        out: list[dict[str, Any]] = []
        while len(out) < limit and ts <= now:
            k = (ts - to_ms(self.ANCHOR)) // step
            rate = 0.0001 + 0.0004 * math.sin(k / 9.0 + self._sym_key(symbol) % 7) * math.sin(k / 31.0)
            out.append({"timestamp": ts, "fundingRate": round(rate, 8)})
            ts += step
        return out

    async def close(self) -> None:
        return None


def default_history_exchange_factory(clock: Clock) -> ExchangeFactory:
    def factory(exchange_id: str) -> Any:
        if exchange_id == "simulated":
            return SimulatedHistoryExchange(clock)
        import ccxt.async_support as ccxt_async

        cls = getattr(ccxt_async, exchange_id)
        return cls({"enableRateLimit": True})  # public endpoints only, no credentials

    return factory


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items() if k != "info"}
    if isinstance(obj, list | tuple):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    return obj if isinstance(obj, str | int | float | bool) or obj is None else str(obj)


def funding_rate_at(points: list[tuple[datetime, Decimal]], ts: datetime) -> Decimal | None:
    """Most recent funding rate at or before ts (no lookahead)."""
    lo, hi, best = 0, len(points) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= ts:
            best = points[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def expected_bars(start: datetime, end: datetime, tf: str) -> int:
    return max(0, int((end - start) / timedelta(seconds=tf_seconds(tf))))
