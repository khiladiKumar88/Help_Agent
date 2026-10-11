"""Harness for the simulator-fairness tests: a driftless world and a metronome strategy.

The point of these helpers is to build a market with **no edge in it at all**, so that any
expectancy the backtester reports must be a bias in the backtester itself.

Why not reuse `SimulatedHistoryExchange`? It deliberately contains trends, volatility regimes
and mean reversion towards an anchor, which are all real signals. Mean reversion in particular
would hand a predictable edge (or penalty) to a fixed-R:R exit, so it cannot be used to measure
whether the *machinery* is fair.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from papermind.core.types import Direction
from papermind.data.history import HistoryStore
from papermind.strategies.base import SignalIntent, Strategy, StrategyContext

D = Decimal
ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Walk:
    """A driftless random walk, stored as 1m OHLCV bars whose high/low are the true envelope."""

    symbol: str
    start: datetime
    end: datetime
    bars: int


def driftless_1m(
    store: HistoryStore,
    symbol: str = "BTC/USDT:USDT",
    exchange: str = "fairness",
    days: int = 60,
    start_price: float = 65_000.0,
    annual_vol: float = 0.6,
    seed: int = 20260101,
    substeps: int = 20,
) -> Walk:
    """Write `days` of driftless 1m candles and the market spec they need.

    Construction, and why each piece matters:
      * log-price increments are iid zero-mean Gaussian — **no drift, no mean reversion**, so the
        probability of touching +rr*R before -1R is exactly rr/(1+rr) and the expectancy of any
        fixed-R:R exit is zero;
      * each 1m bar is built from `substeps` sub-minute steps, so its high and low are the real
        envelope of the path that the broker's intrabar walk is meant to approximate. Drawing
        high/low independently of open/close would invent paths that never happened and make the
        fill simulation look better or worse than it is;
      * everything is seeded, so a failure is reproducible.
    """
    minutes = days * 24 * 60
    steps = minutes * substeps
    rng = np.random.default_rng(seed)
    sigma_per_step = annual_vol / math.sqrt(365 * 24 * 60 * substeps)
    increments = rng.normal(0.0, sigma_per_step, size=steps)
    path = start_price * np.exp(np.cumsum(increments))
    path = np.concatenate([[start_price], path])  # include the opening price

    per_bar = path[1:].reshape(minutes, substeps)
    opens = np.concatenate([[start_price], per_bar[:-1, -1]])
    closes = per_bar[:, -1]
    highs = np.maximum(per_bar.max(axis=1), opens)
    lows = np.minimum(per_bar.min(axis=1), opens)

    start = ANCHOR
    rows: list[list[Any]] = []
    for i in range(minutes):
        ts = int((start + timedelta(minutes=i)).timestamp() * 1000)
        rows.append([ts, float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i]), 10.0])

    store.upsert_candles(exchange, symbol, "1m", rows)
    # aggregate the same path into the coarser bars the comparison needs, so every timeframe
    # describes one identical world
    for tf, span in (("5m", 5), ("15m", 15)):
        agg: list[list[Any]] = []
        for i in range(0, minutes - span + 1, span):
            block = rows[i : i + span]
            agg.append(
                [
                    block[0][0],
                    block[0][1],
                    max(b[2] for b in block),
                    min(b[3] for b in block),
                    block[-1][4],
                    sum(b[5] for b in block),
                ]
            )
        store.upsert_candles(exchange, symbol, tf, agg)
    store.save_market(exchange, symbol, _market_spec(symbol))
    return Walk(symbol=symbol, start=start, end=start + timedelta(minutes=minutes), bars=minutes)


def _market_spec(symbol: str) -> dict[str, Any]:
    base, rest = symbol.split("/")
    quote = rest.split(":")[0]
    return {
        "id": symbol,
        "symbol": symbol,
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
        "precision": {"price": 0.1, "amount": 0.001},
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 1}},
        "_precision_mode": 4,
    }


class Metronome(Strategy):
    """Fires on a fixed cadence with a fixed stop distance and R:R — no market opinion at all.

    It exists only to give the random-entry baseline something to mirror. Its own direction
    alternates, which does not matter: the fairness assertions are made on the BASELINE, whose
    direction is a seeded coin flip.
    """

    id = "fairness_metronome"
    name = "Metronome (test only)"
    description = "Signals every N bars with a fixed stop distance; used to measure simulator bias."
    default_params = {"every": 4, "stop_pct": 0.4, "rr": 2.0}
    param_grid = {"every": [4]}

    @property
    def warmup_bars(self) -> int:
        return 2

    def reset(self) -> None:
        self._n = 0

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        n = getattr(self, "_n", 0)
        self._n = n + 1
        if n % self.pi("every"):
            return None
        close = ctx.frame.last(ctx.frame.close)
        if math.isnan(close) or close <= 0:
            return None
        direction = Direction.LONG if (n // self.pi("every")) % 2 == 0 else Direction.SHORT
        distance = close * self.p("stop_pct") / 100.0
        stop = close - distance if direction is Direction.LONG else close + distance
        return self.build(ctx, direction, stop, self.p("rr"), "metronome")


def frictionless_book(book: dict[str, Any], **over: Any) -> dict[str, Any]:
    """Permissive risk limits, so the fairness sample is not censored.

    A daily-loss halt or a trades-per-day cap would truncate the losing tail and make a biased
    simulator look fair, so they are lifted here — and ONLY here.
    """
    risk = {
        **book["risk"],
        "max_risk_per_trade_pct": "1",
        "max_open_positions": 50,
        "max_trades_per_day": 500,
        "daily_loss_limit_pct": "100000",
        "flatten_on_daily_loss": False,
    }
    return {**book, "risk": risk, "session": None, **over}
