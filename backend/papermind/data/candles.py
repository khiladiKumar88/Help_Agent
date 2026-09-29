"""Candle aggregation: closed base candles (1m live; 1m/5m/... in replays) -> higher timeframes.

Only CLOSED candles are ever emitted to consumers (strategies, backtests) — the forming
candle is exposed separately for the UI. This is what keeps signals free of lookahead.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from papermind.core.money import ZERO
from papermind.core.types import Candle, Tick

_UNITS = {"m": 60, "h": 3600, "d": 86400, "D": 86400, "w": 604800}


def tf_seconds(tf: str) -> int:
    try:
        n, unit = int(tf[:-1]), tf[-1]
        return n * _UNITS[unit]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"bad timeframe '{tf}'") from exc


def bucket_start(ts: datetime, tf: str, offset: timedelta = timedelta(0)) -> datetime:
    """Floor ts to the tf bucket. `offset` shifts bucket boundaries (e.g. IST sessions)."""
    secs = tf_seconds(tf)
    epoch = int((ts.astimezone(UTC) - offset).timestamp())
    return datetime.fromtimestamp(epoch - epoch % secs, UTC) + offset


@dataclass
class _Forming:
    ts_open: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    last_minute: datetime

    def to_candle(self, instrument_id: str, tf: str, closed: bool) -> Candle:
        return Candle(
            instrument_id=instrument_id,
            timeframe=tf,
            ts_open=self.ts_open,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            closed=closed,
        )


@dataclass
class CandleBuilder:
    timeframes: list[str] = field(default_factory=lambda: ["5m", "15m", "1h", "4h", "1d"])
    history: int = 2000
    close_on_tick_rollover: bool = False  # simulated feeds: ticks define the base candles
    base_tf: str = "1m"
    _hist: dict[tuple[str, str], deque[Candle]] = field(default_factory=dict)
    _floats: dict[tuple[str, str], deque[tuple[float, float, float, float, float]]] = field(default_factory=dict)
    _agg: dict[tuple[str, str], _Forming] = field(default_factory=dict)
    _tick_1m: dict[str, _Forming] = field(default_factory=dict)

    # ---- queries
    def closed(self, instrument_id: str, tf: str, limit: int | None = None) -> list[Candle]:
        dq = self._hist.get((instrument_id, tf))
        if not dq:
            return []
        items = list(dq)
        return items[-limit:] if limit else items

    def last_closed(self, instrument_id: str, tf: str) -> Candle | None:
        dq = self._hist.get((instrument_id, tf))
        return dq[-1] if dq else None

    def forming(self, instrument_id: str) -> Candle | None:
        f = self._tick_1m.get(instrument_id)
        return f.to_candle(instrument_id, self.base_tf, closed=False) if f else None

    def _push(self, c: Candle) -> None:
        key = (c.instrument_id, c.timeframe)
        dq = self._hist.setdefault(key, deque(maxlen=self.history))
        if dq and c.ts_open <= dq[-1].ts_open:
            return  # duplicate / out of order: ignore
        dq.append(c)
        fq = self._floats.setdefault(key, deque(maxlen=self.history))
        fq.append((float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume)))

    def closed_with_floats(
        self, instrument_id: str, tf: str, limit: int
    ) -> tuple[list[Candle], list[tuple[float, float, float, float, float]]]:
        """Closed candles plus their cached float OHLCV (converted once per candle, not once per bar)."""
        key = (instrument_id, tf)
        dq, fq = self._hist.get(key), self._floats.get(key)
        if not dq or not fq:
            return [], []
        n = min(limit, len(dq))
        return list(dq)[-n:], list(fq)[-n:]

    # ---- inputs
    def on_1m_close(self, c: Candle) -> list[Candle]:
        return self.on_base_close(c)

    def on_base_close(self, c: Candle) -> list[Candle]:
        """Feed a CLOSED base-timeframe candle. Returns every candle that closed as a result (base first)."""
        if c.timeframe != self.base_tf or not c.closed:
            raise ValueError(f"expected closed {self.base_tf} candles, got {c.timeframe}")
        last = self.last_closed(c.instrument_id, self.base_tf)
        if last is not None and c.ts_open <= last.ts_open:
            return []
        self._push(c)
        out = [c]
        minute_end = c.ts_open + timedelta(seconds=tf_seconds(self.base_tf))
        base_s = tf_seconds(self.base_tf)
        for tf in self.timeframes:
            if tf_seconds(tf) <= base_s or tf_seconds(tf) % base_s:
                continue  # only strict multiples of the base timeframe are aggregated
            key = (c.instrument_id, tf)
            b0 = bucket_start(c.ts_open, tf)
            cur = self._agg.get(key)
            if cur is not None and cur.ts_open != b0:
                # gap: previous bucket never saw its final minute — close it as-is
                done = cur.to_candle(c.instrument_id, tf, closed=True)
                self._push(done)
                out.append(done)
                cur = None
            if cur is None:
                cur = _Forming(b0, c.open, c.high, c.low, c.close, c.volume, c.ts_open)
                self._agg[key] = cur
            else:
                cur.high = max(cur.high, c.high)
                cur.low = min(cur.low, c.low)
                cur.close = c.close
                cur.volume += c.volume
                cur.last_minute = c.ts_open
            if minute_end >= b0 + timedelta(seconds=tf_seconds(tf)):
                done = cur.to_candle(c.instrument_id, tf, closed=True)
                self._push(done)
                out.append(done)
                del self._agg[key]
        return out

    def on_tick(self, t: Tick) -> list[Candle]:
        """Update the forming 1m candle. If close_on_tick_rollover, returns closed candles."""
        m0 = bucket_start(t.ts, self.base_tf)
        f = self._tick_1m.get(t.instrument_id)
        closed: list[Candle] = []
        vol = t.volume or ZERO
        if f is not None and m0 > f.ts_open:
            if self.close_on_tick_rollover:
                closed = self.on_base_close(f.to_candle(t.instrument_id, self.base_tf, closed=True))
            f = None
        if f is None:
            self._tick_1m[t.instrument_id] = _Forming(m0, t.ltp, t.ltp, t.ltp, t.ltp, vol, m0)
        elif m0 == f.ts_open:
            f.high = max(f.high, t.ltp)
            f.low = min(f.low, t.ltp)
            f.close = t.ltp
            f.volume += vol
        return closed

    def seed_timeframe(self, candles: list[Candle]) -> None:
        """Load authoritative closed higher-timeframe candles (oldest first), e.g. from the exchange.

        Call before seed(): later aggregated duplicates of the same buckets are ignored.
        """
        for c in candles:
            if c.closed:
                self._push(c)

    def seed(self, candles: list[Candle]) -> None:
        """Load historical closed base candles (oldest first) without emitting anything."""
        for c in candles:
            self.on_base_close(c)
