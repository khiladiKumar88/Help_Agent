"""FeatureFrame: lazily computed, memoized indicators over a window of CLOSED candles.

One frame is built per (instrument, timeframe) per candle close and shared by the regime
detector and every strategy, so each indicator is computed at most once per bar.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from papermind.core.types import Candle
from papermind.strategies import indicators as ind
from papermind.strategies.indicators import Arr


class FeatureFrame:
    def __init__(
        self,
        candles: list[Candle],
        session_tz: str = "UTC",
        floats: list[tuple[float, float, float, float, float]] | None = None,
    ) -> None:
        if not candles:
            raise ValueError("FeatureFrame needs at least one candle")
        self.candles = candles
        self.tz = session_tz
        self.ts: list[datetime] = [c.ts_open for c in candles]
        if floats is None or len(floats) != len(candles):
            floats = [(float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume)) for c in candles]
        arr = np.array(floats, dtype=np.float64)
        self.open, self.high, self.low, self.close, self.volume = (np.ascontiguousarray(arr[:, i]) for i in range(5))
        self._cache: dict[tuple[Any, ...], Any] = {}

    def __len__(self) -> int:
        return len(self.candles)

    def _memo(self, key: tuple[Any, ...], fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ------------------------------------------------------------------ indicators
    def ema(self, n: int) -> Arr:
        out: Arr = self._memo(("ema", n), lambda: ind.ema(self.close, n))
        return out

    def sma(self, n: int) -> Arr:
        out: Arr = self._memo(("sma", n), lambda: ind.sma(self.close, n))
        return out

    def rsi(self, n: int = 14) -> Arr:
        out: Arr = self._memo(("rsi", n), lambda: ind.rsi(self.close, n))
        return out

    def atr(self, n: int = 14) -> Arr:
        out: Arr = self._memo(("atr", n), lambda: ind.atr(self.high, self.low, self.close, n))
        return out

    def adx(self, n: int = 14) -> tuple[Arr, Arr, Arr]:
        out: tuple[Arr, Arr, Arr] = self._memo(("adx", n), lambda: ind.adx(self.high, self.low, self.close, n))
        return out

    def supertrend(self, n: int = 10, mult: float = 3.0) -> tuple[Arr, Arr]:
        out: tuple[Arr, Arr] = self._memo(
            ("st", n, mult), lambda: ind.supertrend(self.high, self.low, self.close, n, mult)
        )
        return out

    def session_ids(self) -> Any:
        tz = ZoneInfo(self.tz)
        return self._memo(
            ("sid",), lambda: np.array([t.astimezone(tz).date().toordinal() for t in self.ts], dtype=np.int64)
        )

    def vwap(self) -> Arr:
        out: Arr = self._memo(
            ("vwap",), lambda: ind.vwap(self.high, self.low, self.close, self.volume, self.session_ids())
        )
        return out

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def last(x: Arr, back: int = 0) -> float:
        i = len(x) - 1 - back
        return float(x[i]) if i >= 0 else float("nan")

    def snapshot(self) -> dict[str, float | None]:
        """Compact numeric context for signals/episodes (JSON-safe; NaN -> None)."""
        a, pdi, mdi = self.adx(14)
        vals = {
            "close": self.last(self.close),
            "ema9": self.last(self.ema(9)),
            "ema21": self.last(self.ema(21)),
            "ema50": self.last(self.ema(50)),
            "rsi14": self.last(self.rsi(14)),
            "atr14": self.last(self.atr(14)),
            "adx14": self.last(a),
            "plus_di": self.last(pdi),
            "minus_di": self.last(mdi),
            "vwap": self.last(self.vwap()),
        }
        return {k: (None if math.isnan(v) else round(v, 6)) for k, v in vals.items()}
