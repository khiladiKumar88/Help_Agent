"""Rule-based regime detector (v1).

Label (first match wins):
  high_volatility  ATR% >= atr_expansion_high x its median over the lookback, OR realized vol >= rv_high,
                   OR India VIX >= vix_high (when available)
  trending_up      ADX >= adx_trend and EMA50 slope >= +slope_pct
  trending_down    ADX >= adx_trend and EMA50 slope <= -slope_pct
  ranging          otherwise
Also tags a time-of-day bucket and the expiry-day flag. Everything uses closed candles only.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from pydantic import BaseModel, ConfigDict

from papermind.core.config import Market
from papermind.strategies import indicators as ind
from papermind.strategies.features import FeatureFrame


class RegimeLabel(StrEnum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    UNKNOWN = "unknown"  # not enough history


class RegimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    adx_len: int = 14
    adx_trend: float = 25.0
    slope_ema: int = 50
    slope_bars: int = 10
    slope_pct: float = 0.3
    atr_rank_lookback: int = 100
    atr_expansion_high: float = 1.6
    rv_bars: int = 48
    rv_high: float = 1.2  # annualized (crypto); India uses VIX
    vix_high: float = 20.0
    min_bars: int = 60


@dataclass(frozen=True)
class Regime:
    label: RegimeLabel
    adx: float | None
    ema_slope_pct: float | None
    atr_pct: float | None  # ATR as % of price
    atr_pct_rank: float | None
    atr_expansion: float | None  # ATR% / median ATR% over the lookback
    realized_vol: float | None
    vix: float | None
    time_bucket: str
    expiry_day: bool

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = str(self.label)
        return d


# India buckets (IST) and crypto buckets (UTC sessions)
_INDIA = [
    (time(9, 15), "pre_open"),
    (time(10, 15), "open"),
    (time(12, 0), "morning"),
    (time(13, 30), "midday"),
    (time(14, 30), "afternoon"),
    (time(15, 30), "close"),
]
_CRYPTO = [(time(8, 0), "asia"), (time(13, 0), "europe"), (time(21, 0), "us"), (time(23, 59, 59), "late")]


def time_bucket(now: datetime, market: Market) -> str:
    if market is Market.INDIA:
        t = now.astimezone(ZoneInfo("Asia/Kolkata")).time()
        for limit, name in _INDIA:
            if t < limit:
                return name
        return "post_close"
    t = now.astimezone(ZoneInfo("UTC")).time()
    for limit, name in _CRYPTO:
        if t < limit:
            return name
    return "late"


def _f(x: float) -> float | None:
    return None if math.isnan(x) else round(x, 6)


def periods_per_year(tf_seconds: int, market: Market) -> float:
    if market is Market.INDIA:
        return 252 * 6.25 * 3600 / tf_seconds  # ~6h15m trading day
    return 365 * 86400 / tf_seconds


def detect_regime(
    frame: FeatureFrame,
    now: datetime,
    market: Market,
    tf_seconds: int,
    cfg: RegimeConfig | None = None,
    vix: float | None = None,
    expiry_day: bool = False,
) -> Regime:
    cfg = cfg or RegimeConfig()
    bucket = time_bucket(now, market)
    if len(frame) < cfg.min_bars:
        return Regime(RegimeLabel.UNKNOWN, None, None, None, None, None, None, vix, bucket, expiry_day)
    a, _, _ = frame.adx(cfg.adx_len)
    adx_v = frame.last(a)
    slope = ind.pct_change(frame.ema(cfg.slope_ema), cfg.slope_bars)
    atr_arr = frame.atr(14)
    atr_pct_arr = atr_arr / frame.close * 100.0
    atr_pct = frame.last(atr_pct_arr)
    rank = ind.percentile_rank(atr_pct_arr, cfg.atr_rank_lookback)
    window = atr_pct_arr[-cfg.atr_rank_lookback :]
    window = window[~np.isnan(window)]
    med = float(np.median(window)) if len(window) else float("nan")
    expansion = atr_pct / med if med and not math.isnan(med) and not math.isnan(atr_pct) else float("nan")
    rv = ind.realized_vol(frame.close, cfg.rv_bars, periods_per_year(tf_seconds, market))

    if (
        (not math.isnan(expansion) and expansion >= cfg.atr_expansion_high)
        or (not math.isnan(rv) and rv >= cfg.rv_high)
        or (vix is not None and vix >= cfg.vix_high)
    ):
        label = RegimeLabel.HIGH_VOLATILITY
    elif not math.isnan(adx_v) and adx_v >= cfg.adx_trend and not math.isnan(slope) and slope >= cfg.slope_pct:
        label = RegimeLabel.TRENDING_UP
    elif not math.isnan(adx_v) and adx_v >= cfg.adx_trend and not math.isnan(slope) and slope <= -cfg.slope_pct:
        label = RegimeLabel.TRENDING_DOWN
    else:
        label = RegimeLabel.RANGING
    return Regime(label, _f(adx_v), _f(slope), _f(atr_pct), _f(rank), _f(expansion), _f(rv), vix, bucket, expiry_day)
