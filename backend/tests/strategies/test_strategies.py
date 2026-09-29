"""Strategy plugins: each fires on realistic data, every signal is well-formed, targeted rule checks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

import pytest

from papermind.core.clock import ReplayClock
from papermind.core.config import BookConfig, Market
from papermind.core.types import Candle, Direction
from papermind.data.history import SimulatedHistoryExchange, from_ms, to_ms
from papermind.regime.detector import Regime, RegimeLabel, detect_regime
from papermind.strategies.base import MAX_GRID, Strategy, StrategyContext, expand_grid, grid_size
from papermind.strategies.builtin import STRATEGIES, FundingFade, OpeningRangeBreakout, RsiMeanReversion, make_strategy
from papermind.strategies.features import FeatureFrame
from tests.conftest import BTC, book_dict, ccxt_instruments

SIM_END = datetime(2026, 3, 1, tzinfo=UTC)


@lru_cache(maxsize=1)
def sim_series() -> tuple[list[Candle], list[tuple[datetime, Decimal]]]:
    ex = SimulatedHistoryExchange(ReplayClock(SIM_END))
    since = to_ms(SIM_END - timedelta(days=60))
    rows: list[list[Any]] = []
    while True:
        page = asyncio.run(ex.fetch_ohlcv("BTC/USDT:USDT", "15m", since, 1000))
        if not page:
            break
        rows += page
        since = page[-1][0] + 900_000
    fund = asyncio.run(ex.fetch_funding_rate_history("BTC/USDT:USDT", to_ms(SIM_END - timedelta(days=61)), 1000))
    candles = [
        Candle(
            instrument_id=BTC,
            timeframe="15m",
            ts_open=from_ms(r[0]),
            open=Decimal(r[1]),
            high=Decimal(r[2]),
            low=Decimal(r[3]),
            close=Decimal(r[4]),
            volume=Decimal(r[5]),
        )
        for r in rows
    ]
    return candles, [(from_ms(f["timestamp"]), Decimal(str(f["fundingRate"]))) for f in fund]


BOOK = BookConfig.model_validate(book_dict())
INST = ccxt_instruments()[0]


def ctx_for(
    window: list[Candle], regime: Regime | None = None, funding: Decimal | None = None, book: BookConfig = BOOK
) -> StrategyContext:
    frame = FeatureFrame(window)
    now = window[-1].ts_open + timedelta(minutes=15)
    reg = regime or detect_regime(frame, now, Market.CRYPTO, 900)
    return StrategyContext(
        now=now, book=book, instrument=INST, timeframe="15m", frame=frame, regime=reg, funding_rate=funding
    )


def run(strategy: Strategy, stride: int = 1) -> list[tuple[int, Any]]:
    from papermind.data.history import funding_rate_at

    candles, fund = sim_series()
    out = []
    for t in range(200, len(candles), stride):
        window = candles[t - 199 : t + 1]
        ctx = ctx_for(window, funding=funding_rate_at(fund, window[-1].ts_open + timedelta(minutes=15)))
        sig = strategy.evaluate(ctx)
        if sig is not None:
            out.append((t, sig))
    return out


def assert_valid(sig: Any) -> None:
    s = sig.direction.sign
    assert (sig.entry_ref - sig.stop_loss) * s > 0, "stop on wrong side"
    if sig.target is not None:
        assert (sig.target - sig.entry_ref) * s > 0, "target on wrong side"
        assert sig.target % INST.tick_size == 0
    assert sig.stop_loss % INST.tick_size == 0
    assert sig.setup


@pytest.mark.parametrize("sid", sorted(STRATEGIES))
def test_each_strategy_fires_with_valid_levels(sid: str) -> None:
    strat = make_strategy(sid)
    sigs = run(strat, stride=1 if sid in ("orb", "funding_fade") else 2)
    assert sigs, f"{sid} produced no signals on 60 days of data"
    for _, sig in sigs:
        assert_valid(sig)
    assert {s.direction for _, s in sigs} <= {Direction.LONG, Direction.SHORT}


def test_evaluation_is_deterministic() -> None:
    a = [(t, s.stop_loss, s.direction) for t, s in run(make_strategy("supertrend_flip"), 3)]
    b = [(t, s.stop_loss, s.direction) for t, s in run(make_strategy("supertrend_flip"), 3)]
    assert a == b


def test_orb_one_signal_per_day_and_synthetic_breakout() -> None:
    day = datetime(2026, 1, 5, tzinfo=UTC)
    closes = [100.0] * 60  # previous day flat (warm-up)
    candles: list[Candle] = []
    start = day - timedelta(minutes=15 * 60)
    for i, c in enumerate(closes):
        candles.append(
            Candle(
                instrument_id=BTC,
                timeframe="15m",
                ts_open=start + timedelta(minutes=15 * i),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal(str(c)),
            )
        )
    # 00:00-00:30 opening range 99..102, then a breakout close at 104
    for i, (o, h, lo, c) in enumerate(
        [(100, 102, 99, 101), (101, 102, 100, 101.5), (101.5, 101.8, 101, 101.2), (101.2, 104.5, 101, 104)]
    ):
        candles.append(
            Candle(
                instrument_id=BTC,
                timeframe="15m",
                ts_open=day + timedelta(minutes=15 * i),
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(lo)),
                close=Decimal(str(c)),
            )
        )
    orb = OpeningRangeBreakout({"range_minutes": 30, "rr": 2.0, "buffer_atr": 0.0})
    assert orb.evaluate(ctx_for(candles[:-2])) is None  # range not complete yet
    sig = orb.evaluate(ctx_for(candles))
    assert sig is not None and sig.direction is Direction.LONG
    assert sig.stop_loss == Decimal("99") and sig.target == Decimal("114")  # 104 + 2 * (104 - 99)
    assert orb.evaluate(ctx_for(candles)) is None  # once per day
    orb.reset()
    assert orb.evaluate(ctx_for(candles)) is not None


def test_orb_uses_india_session_open() -> None:
    india = BookConfig.model_validate(
        {
            **book_dict(),
            "session": {
                "timezone": "Asia/Kolkata",
                "market_open": "09:15",
                "entry_start": "09:20",
                "entry_end": "15:00",
            },
        }
    )
    open_utc = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)  # 09:15 IST
    candles = [
        Candle(
            instrument_id=BTC,
            timeframe="15m",
            ts_open=open_utc - timedelta(minutes=15 * (60 - i)),
            open=Decimal("100"),
            high=Decimal("100.5"),
            low=Decimal("99.5"),
            close=Decimal("100"),
        )
        for i in range(60)
    ]
    candles += [
        Candle(
            instrument_id=BTC,
            timeframe="15m",
            ts_open=open_utc,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
        ),
        Candle(
            instrument_id=BTC,
            timeframe="15m",
            ts_open=open_utc + timedelta(minutes=15),
            open=Decimal("100.5"),
            high=Decimal("100.6"),
            low=Decimal("97"),
            close=Decimal("97.5"),
        ),
    ]
    sig = OpeningRangeBreakout({"range_minutes": 15, "buffer_atr": 0.0}).evaluate(ctx_for(candles, book=india))
    assert sig is not None and sig.direction is Direction.SHORT and sig.stop_loss == Decimal("101")


def test_rsi_mr_requires_ranging_regime() -> None:
    candles, _ = sim_series()
    strat = RsiMeanReversion()
    trending = Regime(RegimeLabel.TRENDING_UP, 30, 1, 1, 0.5, 1, 0.5, None, "us", False)
    hits = 0
    for t in range(200, len(candles), 2):
        window = candles[t - 199 : t + 1]
        assert strat.evaluate(ctx_for(window, regime=trending)) is None
        hits += strat.evaluate(ctx_for(window)) is not None
    assert hits > 0


def test_funding_fade_needs_funding_and_perp() -> None:
    candles, _ = sim_series()
    strat = FundingFade()
    window = candles[-200:]
    assert strat.evaluate(ctx_for(window, funding=None)) is None
    assert Market.INDIA not in strat.markets


def test_param_validation_and_grids() -> None:
    with pytest.raises(ValueError, match="unknown params"):
        make_strategy("orb", {"nope": 1})
    with pytest.raises(KeyError):
        make_strategy("does_not_exist")
    for cls in STRATEGIES.values():
        assert grid_size(cls.param_grid) <= MAX_GRID
        combos = expand_grid(cls.param_grid)
        assert combos and all(set(c) <= set(cls.default_params) for c in combos)
        assert cls.description and cls.name
    with pytest.raises(ValueError, match="too large"):
        expand_grid({"a": list(range(5)), "b": list(range(5))})
    with pytest.raises(ValueError, match="too large"):

        class Huge(Strategy):
            id = "huge"
            name = description = "x"
            default_params = {"a": 1}
            param_grid = {"a": list(range(40))}

            def evaluate(self, ctx: StrategyContext) -> None:
                return None


def test_build_rejects_bad_levels() -> None:
    candles, _ = sim_series()
    ctx = ctx_for(candles[-200:])
    close = float(candles[-1].close)
    assert Strategy.build(ctx, Direction.LONG, close + 10, 2.0, "x") is None  # stop above entry
    assert Strategy.build(ctx, Direction.LONG, float("nan"), 2.0, "x") is None
    assert Strategy.build(ctx, Direction.LONG, close - 10, None, "x", target=close - 1) is None
    assert Strategy.build(ctx, Direction.LONG, close - 10, None, "x", target=float("nan")) is None
    s = Strategy.build(ctx, Direction.SHORT, close + 10.04, None, "x")
    assert (
        s is not None and s.target is None and s.stop_loss % INST.tick_size == 0 and s.stop_loss > Decimal(str(close))
    )
