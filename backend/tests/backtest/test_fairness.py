"""End-to-end simulator fairness: a driftless market run through the WHOLE pipeline.

This is the integration half of the fairness check. It builds a market with no edge in it,
stores it as candles, and drives data -> scanner -> baseline agent -> broker -> metrics, then
asserts the reported expectancy is what arithmetic demands:

  * zero costs        -> expectancy ~= 0R   (gambler's ruin: P(+rr*R before -1R) = 1/(1+rr))
  * costs switched on -> expectancy ~= -(cost in R), and never worse

Precision lives in `test_fairness_broker.py`, which drives the broker directly with many more
trades; at the few hundred trades an end-to-end run produces, sampling noise on expectancy is
already about +/-0.2R. The job here is to prove nothing in the surrounding pipeline (candle
building, signal timing, sizing, journalling) reintroduces a bias.

Together with the broker test this is the only place in the suite allowed to run with zero fees
and zero slippage; a test below confirms every normal path still refuses them.
"""

from __future__ import annotations

import asyncio
import math
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from papermind.backtest.runner import BacktestError, _RangeSpec, run_engine
from papermind.data.history import HistoryStore
from papermind.journal.entities import Trade
from tests.backtest.fairness import Metronome, Walk, driftless_1m, frictionless_book
from tests.conftest import BOOK_ID, book_dict, make_cfg

D = Decimal
EXCHANGE = "fairness"
DAYS = 20  # ~270 baseline trades: enough for the integration check, fast enough to live in `check`

ZERO_CHARGES: dict[str, Any] = {
    "kind": "crypto",
    "source_url": "https://example.test/fairness",
    "as_of": "2026-01-01",
    "maker_pct": "0",
    "taker_pct": "0",
}
REAL_CHARGES: dict[str, Any] = {**ZERO_CHARGES, "maker_pct": "0.02", "taker_pct": "0.05"}

NO_SLIPPAGE = {"model": "bps", "bps": "0", "est_spread_bps": "0"}
REAL_SLIPPAGE = {"model": "bps", "bps": "2", "est_spread_bps": "2"}


@pytest.fixture(scope="module")
def walk(tmp_path_factory: pytest.TempPathFactory) -> tuple[HistoryStore, Walk]:
    path: Path = tmp_path_factory.mktemp("fairness") / "walk.db"
    store = HistoryStore(f"sqlite:///{path}")
    w = driftless_1m(store, exchange=EXCHANGE, days=DAYS)
    return store, w


def cfg_for(charges: dict[str, Any], slippage: dict[str, Any]) -> Any:
    book = frictionless_book(book_dict(), slippage=slippage, charges="fairness_charges")
    book["instruments"] = ["BTC/USDT:USDT"]
    book["baseline"] = {"enabled": True, "seed": 11, "max_delay_bars": 2}
    book["shadow_raw_strategy"] = False
    return make_cfg(book, charges={"fairness_charges": charges})


async def measure(
    store: HistoryStore,
    w: Walk,
    charges: dict[str, Any],
    slippage: dict[str, Any],
    base_tf: str = "1m",
) -> list[Trade]:
    """Run the metronome + random baseline over the driftless walk; return the BASELINE trades."""
    cfg = cfg_for(charges, slippage)
    spec = _RangeSpec(
        book_id=BOOK_ID,
        exchange=EXCHANGE,
        symbol=w.symbol,
        base_tf=base_tf,
        start=w.start + timedelta(hours=6),  # let the warm-up pass
        end=w.end - timedelta(hours=1),
    )
    out = await run_engine(
        cfg,
        store,
        spec,
        [Metronome()],
        baseline=True,
        allow_zero_costs=True,
    )
    return out.baseline_trades


def stats(trades: list[Trade]) -> tuple[int, float, float]:
    """(n, mean R, standard error of the mean R) over trades that reached a stop or a target.

    Trades still open when the window ends are flattened at market; including them adds noise
    without testing the exit logic, so they are left out.
    """
    rs = [
        float(t.r_multiple)
        for t in trades
        if t.r_multiple is not None and str(t.exit_reason) in ("stop_loss", "target")
    ]
    n = len(rs)
    if n < 2:
        return n, 0.0, 0.0
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1)
    return n, mean, math.sqrt(var / n)


def cost_in_r(trades: list[Trade]) -> float:
    """Average round-trip cost of a trade, expressed in R (fees + funding / initial risk)."""
    vals = [float((t.charges - t.funding) / t.initial_risk) for t in trades if t.initial_risk and t.initial_risk > 0]
    return sum(vals) / len(vals) if vals else 0.0


@pytest.fixture(scope="module")
def frictionless(walk: tuple[HistoryStore, Walk]) -> list[Trade]:
    store, w = walk
    return asyncio.run(measure(store, w, ZERO_CHARGES, NO_SLIPPAGE))


@pytest.fixture(scope="module")
def with_costs(walk: tuple[HistoryStore, Walk]) -> list[Trade]:
    store, w = walk
    return asyncio.run(measure(store, w, REAL_CHARGES, REAL_SLIPPAGE))


class TestFrictionless:
    """Zero fees, zero slippage, driftless walk => expectancy must be indistinguishable from 0R."""

    def test_the_sample_is_big_enough_to_mean_anything(self, frictionless: list[Trade]) -> None:
        n, _mean, _se = stats(frictionless)
        assert n >= 60, f"only {n} stop/target exits — the fairness check needs a real sample"

    def test_expectancy_is_zero_within_statistical_tolerance(self, frictionless: list[Trade]) -> None:
        n, mean, se = stats(frictionless)
        # 4 standard errors: a fair simulator fails this about 1 run in 15,000, and the data is
        # seeded, so a failure here means a real bias rather than bad luck.
        tolerance = 4 * se
        assert abs(mean) <= tolerance, (
            f"random entries on a driftless walk returned {mean:+.4f}R over {n} trades "
            f"(tolerance +/-{tolerance:.4f}R, se {se:.4f}). The simulator has a bias."
        )

    def test_no_cost_was_charged(self, frictionless: list[Trade]) -> None:
        assert abs(cost_in_r(frictionless)) < 1e-6, "this run was supposed to be frictionless"

    def test_wins_and_losses_land_where_the_exit_levels_are(self, frictionless: list[Trade]) -> None:
        """A loser should be ~1R and a winner ~rr*R. Anything worse is the fill model leaking."""
        losers = [float(t.r_multiple) for t in frictionless if t.r_multiple is not None and t.r_multiple < 0]
        winners = [float(t.r_multiple) for t in frictionless if t.r_multiple is not None and t.r_multiple > 0]
        assert losers and winners
        avg_loss = sum(losers) / len(losers)
        # Without costs a stop-out is EXACTLY -1R. This caught the original bug: stops used to
        # fill at the bar's extreme rather than at the stop level, making every loser ~-1.11R
        # while targets still paid their full +rr*R.
        assert -1.02 <= avg_loss <= -0.98, (
            f"average loser was {avg_loss:+.4f}R; a frictionless stop-out is exactly -1R, so the "
            "difference is the fill model giving R away"
        )


class TestWithCosts:
    """Switching fees and slippage on must cost exactly what the model says — no more."""

    def test_expectancy_matches_the_modelled_cost(self, with_costs: list[Trade]) -> None:
        n, mean, se = stats(with_costs)
        assert n >= 60
        expected = -cost_in_r(with_costs)
        tolerance = 4 * se
        assert mean <= expected + tolerance, (
            f"expectancy {mean:+.4f}R is better than the costs allow ({expected:+.4f}R) — "
            "costs are not being charged properly"
        )
        assert mean >= expected - tolerance, (
            f"expectancy {mean:+.4f}R is WORSE than the modelled cost ({expected:+.4f}R, "
            f"tolerance {tolerance:.4f}R). Something beyond fees and slippage is leaking R."
        )

    def test_costs_were_actually_charged(self, with_costs: list[Trade]) -> None:
        assert cost_in_r(with_costs) > 0.01, "fees and slippage should be a visible fraction of R"

    def test_costs_only_make_it_worse(self, frictionless: list[Trade], with_costs: list[Trade]) -> None:
        _n1, free_mean, _ = stats(frictionless)
        _n2, paid_mean, se = stats(with_costs)
        assert paid_mean <= free_mean + 4 * se


class TestZeroCostsStillRefusedEverywhereElse:
    async def test_a_normal_backtest_refuses_zero_fees(self, walk: tuple[HistoryStore, Walk]) -> None:
        store, w = walk
        spec = _RangeSpec(
            book_id=BOOK_ID,
            exchange=EXCHANGE,
            symbol=w.symbol,
            base_tf="1m",
            start=w.start + timedelta(hours=6),
            end=w.start + timedelta(days=2),
        )
        with pytest.raises(BacktestError, match="non-zero trading fees"):
            await run_engine(cfg_for(ZERO_CHARGES, REAL_SLIPPAGE), store, spec, [Metronome()])

    async def test_a_normal_backtest_refuses_zero_slippage(self, walk: tuple[HistoryStore, Walk]) -> None:
        store, w = walk
        spec = _RangeSpec(
            book_id=BOOK_ID,
            exchange=EXCHANGE,
            symbol=w.symbol,
            base_tf="1m",
            start=w.start + timedelta(hours=6),
            end=w.start + timedelta(days=2),
        )
        with pytest.raises(BacktestError, match="non-zero slippage"):
            await run_engine(cfg_for(REAL_CHARGES, NO_SLIPPAGE), store, spec, [Metronome()])
