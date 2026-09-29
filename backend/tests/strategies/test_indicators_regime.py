from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from papermind.core.config import Market
from papermind.core.types import Candle
from papermind.regime.detector import RegimeConfig, RegimeLabel, detect_regime, periods_per_year, time_bucket
from papermind.strategies import indicators as ind
from papermind.strategies.features import FeatureFrame

T0 = datetime(2026, 1, 5, tzinfo=UTC)


def candles(closes: list[float], wick: float = 0.5, tf_min: int = 15, vol: float = 10.0) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            Candle(
                instrument_id="x",
                timeframe=f"{tf_min}m",
                ts_open=T0 + timedelta(minutes=tf_min * i),
                open=Decimal(str(round(o, 4))),
                high=Decimal(str(round(max(o, c) + wick, 4))),
                low=Decimal(str(round(min(o, c) - wick, 4))),
                close=Decimal(str(round(c, 4))),
                volume=Decimal(str(vol)),
            )
        )
        prev = c
    return out


def test_sma_ema_seed_and_constant() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert np.isnan(ind.sma(x, 3)[1]) and ind.sma(x, 3)[2] == 2.0 and ind.sma(x, 3)[4] == 4.0
    e = ind.ema(x, 3)
    assert np.isnan(e[1]) and e[2] == 2.0 and e[3] == pytest.approx(3.0) and e[4] == pytest.approx(4.0)
    assert np.allclose(ind.ema(np.full(20, 7.0), 5)[4:], 7.0)
    assert np.isnan(ind.ema(x, 10)).all() and np.isnan(ind.sma(x, 10)).all()


def test_rsi_hand_computed() -> None:
    r = ind.rsi(np.array([1.0, 2, 3, 2, 3, 4]), 2)
    assert np.isnan(r[:2]).all()
    assert list(r[2:]) == pytest.approx([100.0, 50.0, 75.0, 87.5])
    assert ind.rsi(np.arange(1.0, 30.0), 14)[-1] == 100.0
    assert ind.rsi(np.full(30, 5.0), 14)[-1] == 50.0
    assert np.isnan(ind.rsi(np.array([1.0, 2.0]), 14)).all()


def test_atr_and_true_range() -> None:
    h = np.array([11.0, 12, 13, 14])
    lo = np.array([9.0, 10, 11, 12])
    c = np.array([10.0, 11, 12, 13])
    assert list(ind.true_range(h, lo, c)) == [2.0, 2.0, 2.0, 2.0]
    assert ind.atr(h, lo, c, 2)[-1] == pytest.approx(2.0)
    assert ind.true_range(np.array([10.0, 20]), np.array([9.0, 19]), np.array([9.5, 19.5]))[1] == pytest.approx(10.5)


def test_adx_trend_and_short_input() -> None:
    up = np.arange(100.0, 160.0)
    a, p, m = ind.adx(up + 1, up - 1, up, 14)
    assert a[-1] > 25 and p[-1] > m[-1]
    assert np.isnan(ind.adx(up[:10] + 1, up[:10] - 1, up[:10], 14)[0]).all()


def test_supertrend_flips_on_reversal() -> None:
    c = np.concatenate([np.linspace(100, 130, 40), np.linspace(130, 95, 40)])
    line, d = ind.supertrend(c + 0.5, c - 0.5, c, 10, 3.0)
    assert d[35] == 1.0 and d[-1] == -1.0
    assert line[-1] > c[-1]  # down-trend: line above price


def test_vwap_resets_per_session() -> None:
    h = np.array([2.0, 4.0, 10.0])
    lo = np.array([0.0, 2.0, 8.0])
    c = np.array([1.0, 3.0, 9.0])
    v = np.array([1.0, 1.0, 1.0])
    out = ind.vwap(h, lo, c, v, np.array([1, 1, 2]))
    assert list(out) == pytest.approx([1.0, 2.0, 9.0])


def test_misc_stats() -> None:
    assert ind.pct_change(np.array([100.0, 110.0]), 1) == pytest.approx(10.0)
    assert math.isnan(ind.pct_change(np.array([1.0]), 1))
    assert ind.percentile_rank(np.array([1.0, 2, 3, 4]), 4) == 1.0
    assert math.isnan(ind.percentile_rank(np.array([np.nan]), 4))
    assert ind.realized_vol(np.exp(np.arange(10) * 0.01), 5, 365) == pytest.approx(0.0, abs=1e-12)
    assert math.isnan(ind.realized_vol(np.array([1.0, 2.0]), 5, 365))


def test_feature_frame_memo_and_snapshot() -> None:
    f = FeatureFrame(candles([100 + i * 0.5 for i in range(80)]))
    assert f.ema(9) is f.ema(9)  # memoized
    snap = f.snapshot()
    assert snap["close"] == pytest.approx(139.5) and snap["rsi14"] is not None and set(snap) >= {"adx14", "vwap"}
    assert FeatureFrame.last(np.array([1.0]), 3) != FeatureFrame.last(np.array([1.0]), 3)  # NaN
    with pytest.raises(ValueError):
        FeatureFrame([])


def test_regime_labels() -> None:
    now = T0 + timedelta(days=2)
    up = FeatureFrame(candles([100 * (1.004**i) for i in range(150)], wick=0.2))
    assert detect_regime(up, now, Market.CRYPTO, 900).label is RegimeLabel.TRENDING_UP
    down = FeatureFrame(candles([200 * (0.996**i) for i in range(150)], wick=0.2))
    assert detect_regime(down, now, Market.CRYPTO, 900).label is RegimeLabel.TRENDING_DOWN
    rng = np.random.default_rng(1)
    flat = FeatureFrame(candles(list(100 + np.sin(np.arange(150) / 3) + rng.normal(0, 0.05, 150)), wick=0.3))
    r = detect_regime(flat, now, Market.CRYPTO, 900)
    assert r.label is RegimeLabel.RANGING and r.time_bucket == "asia"
    calm_then_wild = [100.0] * 120 + [100 + (8 if i % 2 else -8) for i in range(30)]
    wild = FeatureFrame(candles(calm_then_wild, wick=0.1))
    assert detect_regime(wild, now, Market.CRYPTO, 900).label is RegimeLabel.HIGH_VOLATILITY
    vix = detect_regime(flat, now, Market.INDIA, 900, vix=25.0, expiry_day=True)
    assert vix.label is RegimeLabel.HIGH_VOLATILITY and vix.expiry_day and vix.as_dict()["label"] == "high_volatility"
    short = FeatureFrame(candles([100.0] * 10))
    assert detect_regime(short, now, Market.CRYPTO, 900, RegimeConfig()).label is RegimeLabel.UNKNOWN


@pytest.mark.parametrize(
    ("utc", "market", "bucket"),
    [
        (datetime(2026, 1, 5, 4, 0, tzinfo=UTC), Market.INDIA, "open"),  # 09:30 IST
        (datetime(2026, 1, 5, 9, 30, tzinfo=UTC), Market.INDIA, "close"),  # 15:00 IST
        (datetime(2026, 1, 5, 3, 0, tzinfo=UTC), Market.INDIA, "pre_open"),
        (datetime(2026, 1, 5, 11, 0, tzinfo=UTC), Market.INDIA, "post_close"),
        (datetime(2026, 1, 5, 14, 0, tzinfo=UTC), Market.CRYPTO, "us"),
        (datetime(2026, 1, 5, 23, 59, 59, 500, tzinfo=UTC), Market.CRYPTO, "late"),
    ],
)
def test_time_buckets(utc: datetime, market: Market, bucket: str) -> None:
    assert time_bucket(utc, market) == bucket


def test_periods_per_year() -> None:
    assert periods_per_year(86400, Market.CRYPTO) == 365
    assert periods_per_year(900, Market.INDIA) == pytest.approx(252 * 25)
